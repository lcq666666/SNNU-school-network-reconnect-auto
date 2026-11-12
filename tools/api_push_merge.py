"""通过 GitHub Git Data API 推送**合并提交**（双亲）到远端分支。

背景与 `api_push.py` 相同：本机 github.com 的 git push 被网络干扰，
api.github.com 畅通。`api_push.py` 只支持单亲线性提交；本脚本专门处理
`git merge` 产生的双亲合并提交：以 ours 的 tree 为 base_tree，上传
merged 与 ours 的差异 blob/tree，创建 parents=[ours, theirs] 的提交，
更新分支 ref，并把本地合并提交重建成与远端完全相同的 SHA
（GitHub 去掉消息末尾换行；用 %aI/%cI 保留时区偏移）。

用法：
    .venv/Scripts/python.exe tools/api_push_merge.py <ours_remote_sha> <theirs_remote_sha> [branch]

凭据：从 ~/.git-credentials 读取（不打印、不入库）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "lcq666666/SNNU-school-network-reconnect-auto"
DEFAULT_BRANCH = "fix/observer-degraded-20261112"


def run(args, **kw):
    return subprocess.check_output(args, cwd=ROOT, **kw)


def git(*args):
    return run(["git", *args]).decode("utf-8").strip()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    ours = sys.argv[1]          # 本分支远端当前 tip（合并第一亲）
    theirs = sys.argv[2]        # 被合并分支远端 tip（合并第二亲）
    branch = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_BRANCH

    head = git("rev-parse", "HEAD")
    parents = git("rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 3 or parents[1] != head:
        print("HEAD 不是双亲合并提交：%r" % (parents,))
        return 2
    p1, p2 = parents[1], parents[2]
    if not head.startswith(p1[:7]) or p1 != ours or p2 != theirs:
        print("亲代校验失败：本地 HEAD 亲代 (%s, %s) 与参数 (%s, %s) 不一致"
              % (p1[:7], p2[:7], ours[:7], theirs[:7]))
        return 2

    cred = open(os.path.expanduser("~/.git-credentials"), encoding="utf-8").read().strip()
    m = re.match(r"https://([^:]+):([^@]+)@github\.com", cred)
    if not m:
        print("cannot parse ~/.git-credentials")
        return 2
    token = m.group(2)

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })

    def api(method, path, **kw):
        r = s.request(method, "https://api.github.com" + path, timeout=60, **kw)
        if r.status_code >= 300:
            print("API ERR", r.status_code, method, path)
            print(r.text[:1000])
            raise SystemExit(1)
        return r.json()

    ours_tree = api("GET", f"/repos/{REPO}/git/commits/{ours}")["tree"]["sha"]
    lines = [ln for ln in git(
        "-c", "core.quotepath=false", "diff-tree",
        "--no-commit-id", "--name-status", "-r", ours, "HEAD").splitlines()
        if ln.strip()]
    entries = []
    for line in lines:
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if status.startswith("D"):
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        content = run(["git", "show", f"HEAD:{path}"]).decode("utf-8")
        blob = api("POST", f"/repos/{REPO}/git/blobs",
                   json={"content": content, "encoding": "utf-8"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = api("POST", f"/repos/{REPO}/git/trees",
               json={"base_tree": ours_tree, "tree": entries})["sha"]
    print("merged tree", tree, "entries", len(entries))

    payload = {
        "message": git("log", "-1", "--format=%B", "HEAD").rstrip("\n"),
        "tree": tree,
        "parents": [ours, theirs],
        "author": {"name": git("log", "-1", "--format=%an"),
                   "email": git("log", "-1", "--format=%ae"),
                   "date": git("log", "-1", "--format=%aI")},
        "committer": {"name": git("log", "-1", "--format=%cn"),
                      "email": git("log", "-1", "--format=%ce"),
                      "date": git("log", "-1", "--format=%cI")},
    }
    remote_sha = api("POST", f"/repos/{REPO}/git/commits", json=payload)["sha"]
    print("uploaded merge", head, "->", remote_sha)

    api("PATCH", f"/repos/{REPO}/git/refs/heads/{branch}", json={"sha": remote_sha, "force": False})
    print("remote ref", branch, "->", remote_sha)

    # 本地重建合并提交以匹配远端 SHA（去尾换行 + 保留时区偏移）
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": git("log", "-1", "--format=%an"),
        "GIT_AUTHOR_EMAIL": git("log", "-1", "--format=%ae"),
        "GIT_AUTHOR_DATE": git("log", "-1", "--format=%aI"),
        "GIT_COMMITTER_NAME": git("log", "-1", "--format=%cn"),
        "GIT_COMMITTER_EMAIL": git("log", "-1", "--format=%ce"),
        "GIT_COMMITTER_DATE": git("log", "-1", "--format=%cI"),
    })
    msg = git("log", "-1", "--format=%B", "HEAD").rstrip("\n")
    local_sha = run(["git", "commit-tree", tree, "-p", p1, "-p", p2],
                    input=msg.encode("utf-8"), env=env).decode("utf-8").strip()
    run(["git", "update-ref", f"refs/heads/{branch}", local_sha])
    run(["git", "update-ref", f"refs/remotes/origin/{branch}", local_sha])
    print("local  ref", branch, "->", local_sha)
    if local_sha != remote_sha:
        print("WARNING: local rebuilt SHA != remote SHA (%s != %s)"
              % (local_sha, remote_sha))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
