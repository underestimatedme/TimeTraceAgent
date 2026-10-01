"""Merge accepted workflow commits only inside the job's isolated worktree."""
import re
import subprocess
from pathlib import Path
from . import worktree

SHA = re.compile(r'^[a-f0-9]{40}$')


def prepare_pipeline_inputs(repo: str, output_path: str, commits: list) -> str:
    if not isinstance(commits, list) or len(commits) > 30 or any(not isinstance(c, str) or not SHA.fullmatch(c) for c in commits):
        raise ValueError('工作流输入必须是最多 30 个完整提交 SHA')
    if Path(repo).resolve() == Path(output_path).resolve():
        raise ValueError('不能在项目主目录汇合工作流输入')
    worktree.verify_metadata(output_path, repo)
    if not worktree.same_repository(output_path, repo):
        raise ValueError('工作流输入目录不属于项目仓库')
    def git(*args):
        try:
            return subprocess.check_output(['git', *worktree.SAFE_GIT, '-c', 'commit.gpgsign=false',
                '-c', 'merge.verifySignatures=false', '-c', 'user.name=TimeTrace Runner',
                '-c', 'user.email=runner@timetrace.local', '-C', output_path, *args],
                stderr=subprocess.STDOUT, text=True).strip()
        except subprocess.CalledProcessError as exc:
            raise ValueError('工作流输入汇合失败；请在任务副本处理冲突或补齐本机提交对象：' + exc.output[-2000:]) from exc
    if git('status', '--porcelain'):
        raise ValueError('任务副本有未提交修改，不能汇合新的工作流输入')
    for commit in sorted(set(commits)):
        if git('cat-file', '-t', commit) != 'commit':
            raise ValueError('本机工作流输入不是提交对象')
    for commit in sorted(set(commits)):
        git('merge', '--no-edit', '--no-stat', commit)
    return git('rev-parse', 'HEAD')


def committed_pipeline_output(repo: str, output_path: str) -> str:
    """The transferable workflow output must contain all implementation work.

    Reserved result scratch is intentionally not part of the implementation.
    Dirty work is preserved; the caller reports failure for manual recovery.
    """
    worktree.verify_metadata(output_path, repo)
    status = subprocess.check_output(['git', *worktree.SAFE_GIT, '-C', output_path,
        'status', '--porcelain=v1', '-z', '--untracked-files=all', '--', '.',
        ':(top,exclude).timetrace/out', ':(top,exclude).timetrace/out.prev-*'])
    if status:
        raise ValueError('任务副本还有未提交的实现，请在副本中提交后重试；修改已保留')
    return worktree.head(output_path, repo)
