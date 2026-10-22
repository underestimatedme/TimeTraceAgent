"""Operator-owned objective contracts; never inferred from model output.

Only explicit finite goals qualify. The external checker and interpreter are
hashed at registration. Optional repository fixtures are pinned as well.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path
from timetrace import checks


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def register(workspace, name, argv, objective, checker, protected=()):
    root = Path(workspace).resolve()
    checker = Path(checker).expanduser().resolve()
    executable = Path(argv[0]).resolve() if argv else root
    if (checks.argv_problem(argv) or not objective.strip() or len(objective) > 4000
            or not os.path.isabs(argv[0]) or not any(os.path.isabs(arg) and Path(arg).resolve() == checker for arg in argv)
            or root == checker or root in checker.parents or root in executable.parents):
        raise ValueError("use an absolute executable and external checker; supply an explicit objective")
    files = {}
    for item in protected:
        path = root / item
        if not item or Path(item).is_absolute() or ".." in Path(item).parts or path.is_symlink() or root not in path.resolve().parents:
            raise ValueError("protected fixtures must be repository-relative regular files")
        files[item] = file_hash(path)
    contract = {"name": name, "objective": objective.strip(), "argv": list(argv),
                "trusted_files": {str(checker): file_hash(checker), str(executable): file_hash(executable)},
                "protected_files": files, "timeout_seconds": 120, "protocol": 1}
    contract["version"] = digest(contract)
    return contract


def valid(contract, argv, cwd=None):
    try:
        copy = dict(contract)
        version = copy.pop("version")
        if digest(copy) != version or contract["argv"] != list(argv):
            return False
        if not all(file_hash(p) == sha for p, sha in contract["trusted_files"].items()):
            return False
        if cwd:
            for p, sha in contract["protected_files"].items():
                path = Path(cwd) / p
                if path.is_symlink() or Path(cwd).resolve() not in path.resolve().parents or file_hash(path) != sha:
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def inventory(db, workspace_id):
    result = []
    for row in db.list_checks(workspace_id):
        contract = db.get_objective_check(workspace_id, row["name"])
        if contract and valid(contract, row["argv"]):
            result.append({key: contract[key] for key in ("name", "objective", "version")})
    return result


def run(contract, argv, cwd, log_file, cancel, **kwargs):
    # Both checks surround execution; a command that rewrites its own checker
    # or tracked source cannot produce a valid committed-snapshot verdict.
    valid_before = valid(contract, argv, cwd)
    if valid_before:
        result = checks.run(contract["name"], argv, cwd, log_file, cancel,
                            timeout=contract["timeout_seconds"], **kwargs)
    else:
        result = checks.CheckResult(contract["name"], 126, "检查器或受保护测试已变化，需要人工核对", 0, False)
    clean = subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=cwd,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    untracked = subprocess.check_output(["git", "ls-files", "--others", "-z"], cwd=cwd).decode().split("\0")
    # Python bytecode is generated from this checkout and discarded with it;
    # new source/config files (including ignored ones) invalidate the snapshot.
    clean = clean and all(not name or ("__pycache__" in Path(name).parts and name.endswith(".pyc"))
                          for name in untracked)
    result.objective_evidence = {
        "contract_version": contract["version"], "snapshot_clean": clean,
        "integrity_valid": valid_before and valid(contract, argv, cwd),
        "failure_class": "assertion" if result.exit_code == 1 and not result.timed_out else
                         ("none" if result.passed else "infrastructure")}
    return result
