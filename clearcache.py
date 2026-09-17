import subprocess, pathlib

root = pathlib.Path(__file__).parent
removed = 0

for p in sorted(root.rglob("__pycache__"), reverse=True):
    try:
        subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(p)], check=True)
        print(f"Removed: {p.relative_to(root)}")
        removed += 1
    except Exception as e:
        print(f"Failed: {p.relative_to(root)} — {e}")

print(f"\nDone — {removed} cache folder(s) deleted.")
