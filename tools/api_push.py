"""通过 GitHub Git Data API 把本地提交推送到远端分支（本机 github.com push 被网络干扰时的替代方案）。

背景：本机直连 github.com 的 git-receive-pack 会被限速/重置（push 挂起），
而 api.github.com 始终畅通。本脚本用官方 Git Data API 上传 blob/tree/commit 并更新分支 ref，
然后把本地分支重建成与远端完全相同的 SHA（GitHub 会去掉提交信息末尾换行，导致 SHA 与本地不同），
从而保证 local == remote、后续可快进推送。

用法：
    .venv/Scripts/python.exe tools/api_push.py <base_sha> [branch]
    # base_sha：远端分支当前 SHA（新提交的父提交起点）
    # branch  ：默认 fix/reconnect-stability-20261013

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
DEFAULT_BRANCH = "fix/reconnect-stability-20261013"


def run(args, **kw):
    return subprocess.check_output(args, cwd=ROOT, **kw)


def git(*args):
    return run(["git", *args]).decode("utf-8").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    base = sys.argv[1]
    branch = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BRANCH

    commits = git("rev-list", "--reverse", f"{base}..HEAD").split()
    if not commits:
        print("no new commits to push")
        return 0

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

    base_tree = api("GET", f"/repos/{REPO}/git/commits/{base}")["tree"]["sha"]
    parent, tree = base, base_tree
    for c in commits:
        lines = [
            ln for ln in git("-c", "core.quotepath=false", "diff-tree",
                             "--no-commit-id", "--name-status", "-r", c).splitlines() if ln.strip()
        ]
        entries = []
        for line in lines:
            parts = line.split("\t")
            status, path = parts[0], parts[-1]
            if status.startswith("D"):
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            content = run(["git", "show", f"{c}:{path}"]).decode("utf-8")
            blob = api("POST", f"/repos/{REPO}/git/blobs",
                       json={"content": content, "encoding": "utf-8"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = api("POST", f"/repos/{REPO}/git/trees",
                   json={"base_tree": tree, "tree": entries})["sha"]
        payload = {
            "message": git("log", "-1", "--format=%B", c).rstrip("\n"),
            "tree": tree,
            "parents": [parent],
            "author": {"name": git("log", "-1", "--format=%an", c),
                       "email": git("log", "-1", "--format=%ae", c),
                       "date": git("log", "-1", "--format=%aI", c)},
            "committer": {"name": git("log", "-1", "--format=%cn", c),
                          "email": git("log", "-1", "--format=%ce", c),
                          "date": git("log", "-1", "--format=%cI", c)},
        }
        parent = api("POST", f"/repos/{REPO}/git/commits", json=payload)["sha"]
        print("uploaded", c, "->", parent)

    api("PATCH", f"/repos/{REPO}/git/refs/heads/{branch}", json={"sha": parent, "force": False})
    print("remote ref", branch, "->", parent)

    # 重建本地提交以匹配远端 SHA（GitHub 去掉消息末尾换行）
    p = base
    for c in commits:
        ctree = git("rev-parse", f"{c}^{{tree}}")
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": git("log", "-1", "--format=%an", c),
            "GIT_AUTHOR_EMAIL": git("log", "-1", "--format=%ae", c),
            "GIT_AUTHOR_DATE": f"{git('log', '-1', '--format=%at', c)} +0000",
            "GIT_COMMITTER_NAME": git("log", "-1", "--format=%cn", c),
            "GIT_COMMITTER_EMAIL": git("log", "-1", "--format=%ce", c),
            "GIT_COMMITTER_DATE": f"{git('log', '-1', '--format=%ct', c)} +0000",
        })
        msg = git("log", "-1", "--format=%B", c).rstrip("\n")
        p = run(["git", "commit-tree", ctree, "-p", p], input=msg.encode("utf-8"),
                env=env).decode("utf-8").strip()
    run(["git", "update-ref", f"refs/heads/{branch}", p])
    run(["git", "update-ref", f"refs/remotes/origin/{branch}", p])
    print("local  ref", branch, "->", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
