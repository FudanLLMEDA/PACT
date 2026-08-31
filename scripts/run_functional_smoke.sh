#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DCP_DIR="${DCP_DIR:-$ROOT/benchmarks}"
OUT_ROOT="${OUT_ROOT:-$ROOT/reproduction-smoke}"
PYTHON="${PYTHON:-python3}"
VIVADO_EXEC="${VIVADO_EXEC:-vivado}"
RUN_SIGNOFF="${RUN_SIGNOFF:-1}"

usage() {
    cat <<'EOF'
Usage: scripts/run_functional_smoke.sh [vexriscv|logicnets]...

Runs the deterministic, no-LLM functional flows provided by
dcp_optimizer.py. With no arguments, both flows run in this order:
VexRiscv, then LogicNets.

Environment:
  DCP_DIR   Directory containing the two input DCPs
  OUT_ROOT  Directory for logs, output DCPs, exit codes, and checksums
  PYTHON    Python interpreter to use
  VIVADO_EXEC  Vivado executable used for independent DCP signoff
  RUN_SIGNOFF  Set to 0 to skip independent signoff (default: 1)
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ "$#" -eq 0 ]]; then
    set -- vexriscv logicnets
fi

mkdir -p "$OUT_ROOT/logs" "$OUT_ROOT/outputs"

run_one() {
    local design="$1"
    local input output log rc_file signoff_dir

    case "$design" in
        vexriscv)
            input="$DCP_DIR/vexriscv_re-place_2025.1.dcp"
            output="$OUT_ROOT/outputs/vexriscv_re-place_2025.1_smoke.dcp"
            ;;
        logicnets)
            input="$DCP_DIR/logicnets_jscl_2025.1.dcp"
            output="$OUT_ROOT/outputs/logicnets_jscl_2025.1_smoke.dcp"
            ;;
        *)
            echo "Unknown design: $design" >&2
            usage >&2
            return 2
            ;;
    esac

    log="$OUT_ROOT/logs/$design.log"
    rc_file="$OUT_ROOT/logs/$design.exit"

    if [[ ! -f "$input" ]]; then
        echo "Missing input DCP: $input" >&2
        return 2
    fi

    {
        echo "design=$design"
        echo "started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "python=$("$PYTHON" --version 2>&1)"
        echo "input=$input"
        sha256sum "$input"
        echo
    } >"$log"

    set +e
    if [[ -x /usr/bin/time ]]; then
        /usr/bin/time -v "$PYTHON" "$ROOT/dcp_optimizer.py" \
            "$input" --test --output "$output" >>"$log" 2>&1
    else
        "$PYTHON" "$ROOT/dcp_optimizer.py" \
            "$input" --test --output "$output" >>"$log" 2>&1
    fi
    local rc=$?
    set -e

    if [[ "$rc" -eq 0 && -s "$output" && "$RUN_SIGNOFF" != "0" ]]; then
        signoff_dir="$OUT_ROOT/signoff/$design"
        mkdir -p "$signoff_dir"
        set +e
        "$VIVADO_EXEC" -mode batch \
            -log "$signoff_dir/vivado.log" \
            -journal "$signoff_dir/vivado.jou" \
            -source "$ROOT/scripts/signoff_checkpoint.tcl" \
            -tclargs "$output" "$signoff_dir" >>"$log" 2>&1
        rc=$?
        set -e
    fi

    printf '%s\n' "$rc" >"$rc_file"
    {
        echo
        echo "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "exit_code=$rc"
        if [[ -f "$output" ]]; then
            sha256sum "$output"
        else
            echo "output_missing=$output"
        fi
    } >>"$log"

    if [[ "$rc" -ne 0 || ! -s "$output" ]]; then
        echo "$design: FAIL (exit=$rc, log=$log)" >&2
        return 1
    fi

    echo "$design: PASS (output=$output, log=$log)"
}

overall_rc=0
for design in "$@"; do
    run_one "$design" || overall_rc=1
done

exit "$overall_rc"
