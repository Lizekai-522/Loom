"""管理公网体验的成员：每人一个访问码和一个具名身份。访问码只写进源码外的私有邀请文件。

新增成员后需重启体验服务和执行器（它们在启动时读取身份文件）；更换访问码与移除成员立即生效。
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets

from bootstrap_local import write_private
from loom.public_access import MEMBERS_SCHEMA, code_hash

TENANT = 'public-preview'


def _paths(work_root):
    private = Path(work_root).resolve() / 'loom-public-preview'
    if not (private / 'identities.json').exists():
        raise ValueError('run prepare_public_preview.py first')
    return private, private / 'identities.json', private / 'members.json', private / 'invites'


def _read(path, default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def _invite(private, invites, subject, code):
    access = json.loads((private / 'access.json').read_bytes())
    invites.mkdir(mode=0o700, exist_ok=True)
    target = invites / (subject + '.txt')
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(access['origin'] + '/console\n访问码：' + code + '\n仅发给本人；不要转发或贴到群里。\n')
    return target


def add(work_root, subject, display=''):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{1,47}', subject) or len(display) > 40:
        raise ValueError('subject: lowercase letters, digits, - or _ (2-48); display: up to 40 characters')
    private, identity_path, members_path, invites = _paths(work_root)
    identities = _read(identity_path, None)
    members = _read(members_path, dict(schema_version=MEMBERS_SCHEMA, members=[]))
    if any(m['subject'] == subject for m in members['members']):
        raise ValueError('member already exists; use rotate to issue a new code')
    if any(r['tenant_id'] == TENANT and r['subject_id'] == subject for r in identities['identities']):
        raise ValueError('an identity with this name already exists')
    identities['identities'].append(dict(tenant_id=TENANT, subject_id=subject, roles=['user'], token=secrets.token_urlsafe(32)))
    code = secrets.token_urlsafe(24)
    members['members'].append(dict(subject=subject, display=display, code_sha256=code_hash(code)))
    write_private(identity_path, identities)
    write_private(members_path, members)
    return dict(added=subject, invite_file=str(_invite(private, invites, subject, code)), restart_required=True)


def rotate(work_root, subject):
    private, _, members_path, invites = _paths(work_root)
    members = _read(members_path, None)
    row = next((m for m in (members or {}).get('members', []) if m['subject'] == subject), None)
    if row is None:
        raise ValueError('no such member')
    code = secrets.token_urlsafe(24)
    row['code_sha256'] = code_hash(code)
    write_private(members_path, members)
    return dict(rotated=subject, invite_file=str(_invite(private, invites, subject, code)), restart_required=False)


def remove(work_root, subject):
    """Signs the person out everywhere. Their identity and history stay for the audit trail."""
    private, _, members_path, invites = _paths(work_root)
    members = _read(members_path, None)
    kept = [m for m in (members or {}).get('members', []) if m['subject'] != subject]
    if members is None or len(kept) == len(members['members']):
        raise ValueError('no such member')
    write_private(members_path, dict(members, members=kept))
    invite = invites / (subject + '.txt')
    if invite.exists():
        invite.unlink()
    return dict(removed=subject, restart_required=False)


def listing(work_root):
    _, _, members_path, _ = _paths(work_root)
    members = _read(members_path, dict(members=[]))
    return dict(members=[dict(subject=m['subject'], display=m.get('display', '')) for m in members['members']])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('add', 'rotate', 'remove', 'list'))
    parser.add_argument('--work-root', required=True)
    parser.add_argument('--subject')
    parser.add_argument('--display', default='')
    args = parser.parse_args()
    try:
        if args.action == 'list':
            result = listing(args.work_root)
        elif not args.subject:
            raise ValueError('--subject required')
        elif args.action == 'add':
            result = add(args.work_root, args.subject, args.display)
        elif args.action == 'rotate':
            result = rotate(args.work_root, args.subject)
        else:
            result = remove(args.work_root, args.subject)
        # Codes never reach stdout; they are only in the private invite file.
        print(json.dumps(dict(passed=True, **result), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps(dict(passed=False, error=str(error)), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
