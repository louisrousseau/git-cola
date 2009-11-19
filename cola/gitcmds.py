"""Provides commands and queries for Git."""
import os
from cStringIO import StringIO

import cola
from cola import gitcmd
from cola import core
from cola import utils

git = gitcmd.instance()


def default_remote():
    """Return the remote tracked by the current branch."""
    branch = current_branch()
    branchconfig = 'branch.%s.remote' % branch
    model = cola.model()
    return model.local_config(branchconfig, 'origin')


def corresponding_remote_ref():
    """Return the remote branch tracked by the current branch."""
    remote = default_remote()
    branch = current_branch()
    best_match = '%s/%s' % (remote, branch)
    remote_branches = branch_list(remote=True)
    if not remote_branches:
        return remote
    for rb in remote_branches:
        if rb == best_match:
            return rb
    if remote_branches:
        return remote_branches[0]
    return remote


def diff_filenames(arg):
    """Return a list of filenames that have been modified"""
    diff_zstr = git.diff(arg, name_only=True, z=True).rstrip('\0')
    return [core.decode(f) for f in diff_zstr.split('\0') if f]


def all_files():
    """Return the names of all files in the repository"""
    return [core.decode(f)
            for f in git.ls_files(z=True)
                        .strip('\0').split('\0') if f]


class _current_branch:
    """Stat cache for current_branch()."""
    st_mtime = 0
    value = None


def current_branch():
    """Find the current branch."""
    model = cola.model()
    head = os.path.abspath(model.git_repo_path('HEAD'))

    try:
        st = os.stat(head)
        if _current_branch.st_mtime == st.st_mtime:
            return _current_branch.value
    except OSError, e:
        pass

    # Handle legacy .git/HEAD symlinks
    if os.path.islink(head):
        refs_heads = os.path.realpath(model.git_repo_path('refs', 'heads'))
        path = os.path.abspath(head).replace('\\', '/')
        if path.startswith(refs_heads + '/'):
            value = path[len(refs_heads)+1:]
            _current_branch.value = value
            _current_branch.st_mtime = st.st_mtime
            return value
        return ''

    # Handle the common .git/HEAD "ref: refs/heads/master" file
    if os.path.isfile(head):
        value = utils.slurp(head).strip()
        ref_prefix = 'ref: refs/heads/'
        if value.startswith(ref_prefix):
            value = value[len(ref_prefix):]

        _current_branch.st_mtime = st.st_mtime
        _current_branch.value = value
        return value

    # This shouldn't happen
    return ''


def branch_list(remote=False):
    """
    Return a list of local or remote branches

    This explicitly removes HEAD from the list of remote branches.

    """
    if remote:
        return for_each_ref_basename('refs/remotes/')
    else:
        return for_each_ref_basename('refs/heads/')


def for_each_ref_basename(refs):
    """Return refs starting with 'refs'."""
    output = git.for_each_ref(refs, format='%(refname)').splitlines()
    non_heads = filter(lambda x: not x.endswith('/HEAD'), output)
    return map(lambda x: x[len(refs):], non_heads)


def tracked_branch(branch=None):
    """Return the remote branch associated with 'branch'."""
    if branch is None:
        branch = current_branch()
    model = cola.model()
    branch_remote = 'local_branch_%s_remote' % branch
    if not model.has_param(branch_remote):
        return ''
    remote = model.param(branch_remote)
    if not remote:
        return ''
    branch_merge = 'local_branch_%s_merge' % branch
    if not model.has_param(branch_merge):
        return ''
    ref = model.param(branch_merge)
    refs_heads = 'refs/heads/'
    if ref.startswith(refs_heads):
        return remote + '/' + ref[len(refs_heads):]
    return ''


def untracked_files():
    """Returns a sorted list of all files, including untracked files."""
    ls_files = git.ls_files(z=True,
                            others=True,
                            exclude_standard=True)
    return [core.decode(f) for f in ls_files.split('\0') if f]


def tag_list():
    """Return a list of tags."""
    tags = for_each_ref_basename('refs/tags/')
    tags.reverse()
    return tags


def commit_diff(sha1):
    commit = git.show(sha1)
    first_newline = commit.index('\n')
    if commit[first_newline+1:].startswith('Merge:'):
        return (core.decode(commit) + '\n\n' +
                core.decode(diff_helper(commit=sha1,
                                             cached=False,
                                             suppress_header=False)))
    else:
        return core.decode(commit)


def diff_helper(commit=None,
                branch=None,
                ref=None,
                endref=None,
                filename=None,
                cached=True,
                with_diff_header=False,
                suppress_header=True,
                reverse=False):
    "Invokes git diff on a filepath."
    if commit:
        ref, endref = commit+'^', commit
    argv = []
    if ref and endref:
        argv.append('%s..%s' % (ref, endref))
    elif ref:
        for r in ref.strip().split():
            argv.append(r)
    elif branch:
        argv.append(branch)

    if filename:
        argv.append('--')
        if type(filename) is list:
            argv.extend(filename)
        else:
            argv.append(filename)

    start = False
    del_tag = 'deleted file mode '

    headers = []
    deleted = cached and not os.path.exists(core.encode(filename))

    diffoutput = git.diff(R=reverse,
                          M=True,
                          no_color=True,
                          cached=cached,
                          # TODO factor our config object
                          unified=cola.model().diff_context,
                          with_raw_output=True,
                          with_stderr=True,
                          *argv)

    # Handle 'git init'
    if diffoutput.startswith('fatal:'):
        if with_diff_header:
            return ('', '')
        else:
            return ''

    output = StringIO()

    diff = diffoutput.split('\n')
    for line in map(core.decode, diff):
        if not start and '@@' == line[:2] and '@@' in line[2:]:
            start = True
        if start or (deleted and del_tag in line):
            output.write(core.encode(line) + '\n')
        else:
            if with_diff_header:
                headers.append(core.encode(line))
            elif not suppress_header:
                output.write(core.encode(line) + '\n')

    result = core.decode(output.getvalue())
    output.close()

    if with_diff_header:
        return('\n'.join(headers), result)
    else:
        return result


def format_patchsets(to_export, revs, output='patches'):
    """
    Group contiguous revision selection into patchsets

    Exists to handle multi-selection.
    Multiple disparate ranges in the revision selection
    are grouped into continuous lists.

    """

    outlines = []

    cur_rev = to_export[0]
    cur_master_idx = revs.index(cur_rev)

    patches_to_export = [[cur_rev]]
    patchset_idx = 0

    # Group the patches into continuous sets
    for idx, rev in enumerate(to_export[1:]):
        # Limit the search to the current neighborhood for efficiency
        master_idx = revs[cur_master_idx:].index(rev)
        master_idx += cur_master_idx
        if master_idx == cur_master_idx + 1:
            patches_to_export[ patchset_idx ].append(rev)
            cur_master_idx += 1
            continue
        else:
            patches_to_export.append([ rev ])
            cur_master_idx = master_idx
            patchset_idx += 1

    # Export each patchsets
    status = 0
    for patchset in patches_to_export:
        newstatus, out = export_patchset(patchset[0],
                                         patchset[-1],
                                         output='patches',
                                         n=len(patchset) > 1,
                                         thread=True,
                                         patch_with_stat=True)
        outlines.append(out)
        if status == 0:
            status += newstatus
    return (status, '\n'.join(outlines))


def export_patchset(start, end, output='patches', **kwargs):
    """Export patches from start^ to end."""
    return git.format_patch('-o', output, start + '^..' + end,
                            with_stderr=True,
                            with_status=True,
                            **kwargs)
