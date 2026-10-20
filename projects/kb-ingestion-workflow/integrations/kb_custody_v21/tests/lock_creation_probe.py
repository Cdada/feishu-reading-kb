"""Four synchronized OS processes; no network, deletions, or application data."""
import json
import multiprocessing as mp
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kb_custody_v21.locking import open_lock


def worker(root, barrier, queue, legacy):
    failures, inodes = [], []
    for i in range(100):
        directory = os.open(str(Path(root) / str(i)), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        barrier.wait(timeout=20)
        try:
            if legacy:
                fd = os.open('.custody.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            else:
                fd = open_lock('.custody.lock', dir_fd=directory)
            inodes.append(os.fstat(fd).st_ino)
            os.close(fd)
        except OSError as exc:
            failures.append({'iteration': i, 'errno': exc.errno, 'directory_links': os.fstat(directory).st_nlink})
        finally:
            os.close(directory)
        barrier.wait(timeout=20)
    queue.put({'failures': failures, 'inodes': inodes})


if __name__ == '__main__':
    root = Path(sys.argv[1])
    for i in range(100):
        (root / str(i)).mkdir()
    barrier, queue = mp.Barrier(4), mp.Queue()
    children = [mp.Process(target=worker, args=(str(root), barrier, queue, '--legacy' in sys.argv)) for _ in range(4)]
    for child in children:
        child.start()
    results = [queue.get(timeout=30) for _ in children]
    for child in children:
        child.join(timeout=10)
    print(json.dumps({'results': results, 'exit_codes': [child.exitcode for child in children]}))
