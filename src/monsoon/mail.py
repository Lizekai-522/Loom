"""Domain mailbox for the resident assistant (IMAP in, SMTP out).

Runs only inside the executor process, the one process that holds secrets. The
mailbox password comes from its own private file; the API process never sees it.

* Receiving: poll the inbox by ``(UIDVALIDITY, UID)`` cursor, parse MIME, keep a
  sanitised and redacted text copy, never execute scripts, load remote content
  or open attachments.
* Sending: only a draft version a named person approved, recorded on Loom's
  effect ledger (intent before the SMTP DATA phase). A delivery whose outcome is
  unknown is never sent again automatically.

Servers are the provider's fixed hosts; no configuration can point the executor
at an arbitrary host.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import email
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime, formatdate, parseaddr
import imaplib
import json
import re
import smtplib
import socket
import ssl
import time
from uuid import uuid4

from loom.store import Conflict, InvalidInput, NotFound, canonical

from .safety import redact
from .web import html_to_text

PROVIDERS = {
    # Alibaba Cloud enterprise mail (mainland and Hong Kong). SMTP: SSL on 465 only.
    'aliyun-qiye': ('imap.qiye.aliyun.com', 'smtp.qiye.aliyun.com'),
    'aliyun-qiye-hk': ('imaphk.qiye.aliyun.com', 'smtphk.qiye.aliyun.com'),
}
ADDRESS = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+')
SUBJECT = re.compile(r'[A-Za-z0-9_.:@-]{1,128}')
MAX_FETCH = 2 * 1024 * 1024        # bytes per message; larger mails are truncated, never dropped silently
MAX_TEXT = 60_000                   # characters of sanitised text kept per message
TIMEOUT = 30
SECRET_NAME = 'MAIL_PASSWORD'
PLACEHOLDERS = ('[手机号]', '[邮箱]', '[身份证号]', '[银行卡号]', '[密钥]', '[私钥]')
MAIL_FAILURE = {
    'MAIL_AUTH_FAILED': '邮箱登录失败：请在执行器私有配置的邮箱密钥文件里更新三方客户端安全密码。',
    'MAIL_SECRET_MISSING': '读不到邮箱密钥文件：请按 mail.secrets_file 放一个只含 MAIL_PASSWORD 的私有文件（权限 600）。',
    'MAIL_NETWORK_UNREACHABLE': '连不上邮件服务器，稍后会自动重试。',
    'MAIL_PROTOCOL_NOT_ENABLED': '邮箱没有开启 IMAP/SMTP 或三方客户端访问：请在阿里邮箱管理后台为这个账号放行。',
    'MAIL_PROTOCOL_ERROR': '邮件服务器返回了无法识别的结果，已停下这一轮同步。',
    'MAIL_MESSAGE_MISSING': '有一封邮件在读取时已被删除，已跳过。',
    'MAIL_REJECTED': '邮件服务器拒绝了这封信（收件人或内容被拒），没有发出。可以修改后重新请求发送。',
    'MAIL_OUTCOME_UNKNOWN': '提交过程中连接中断，不确定服务器是否已接受。不会自动重发：请到网页版邮箱“已发送”里核对。',
    'MAIL_LEDGER_CONFLICT': '账本里已有这次发送的记录，已停下等你核对，不会重发。',
    'MAIL_SEND_DISABLED': '执行器配置里没有开启发信（mail.send），草稿可以复制到网页版邮箱发送。',
    'DRAFT_CHANGED': '草稿在批准后被修改过，需要重新请求发送。',
}


class MailError(Exception):
    """Static code only; never a server response, address or password."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MailConfig:
    provider: str
    address: str
    subject: str
    owner_addresses: tuple = ()
    secrets_file: str = 'mail-secrets.json'
    poll_seconds: int = 60
    initial_days: int = 7
    initial_limit: int = 100
    send: bool = False

    @classmethod
    def parse(cls, data):
        if data is None:
            return None
        allowed = {'provider', 'address', 'subject', 'owner_addresses', 'secrets_file', 'poll_seconds', 'initial_days',
                   'initial_limit', 'send'}
        if type(data) is not dict or not {'provider', 'address', 'subject'} <= set(data) <= allowed:
            raise InvalidInput('mail needs provider, address and subject (keys: ' + ', '.join(sorted(allowed)) + ')')
        if data['provider'] not in PROVIDERS:
            raise InvalidInput('mail provider must be one of: ' + ', '.join(sorted(PROVIDERS)))
        owners = data.get('owner_addresses', [])
        if (type(data['address']) is not str or not ADDRESS.fullmatch(data['address'])
                or type(data['subject']) is not str or not SUBJECT.fullmatch(data['subject'])
                or type(owners) is not list or len(owners) > 8
                or any(type(a) is not str or not ADDRESS.fullmatch(a) for a in owners)):
            raise InvalidInput('invalid mail address, subject or owner_addresses')
        secrets = data.get('secrets_file', 'mail-secrets.json')
        numbers = dict(poll_seconds=(30, 3600), initial_days=(1, 30), initial_limit=(1, 500))
        for key, (low, high) in numbers.items():
            value = data.get(key, cls.__dataclass_fields__[key].default)
            if type(value) is not int or not low <= value <= high:
                raise InvalidInput(f'mail {key} must be between {low} and {high}')
        if type(secrets) is not str or not secrets.strip() or len(secrets) > 4096 or type(data.get('send', False)) is not bool:
            raise InvalidInput('invalid mail secrets_file or send')
        return cls(data['provider'], data['address'].lower(), data['subject'], tuple(a.lower() for a in owners), secrets,
                   data.get('poll_seconds', 60), data.get('initial_days', 7), data.get('initial_limit', 100),
                   data.get('send', False))

    @property
    def imap_host(self):
        return PROVIDERS[self.provider][0]

    @property
    def smtp_host(self):
        return PROVIDERS[self.provider][1]

    @property
    def domain(self):
        return self.address.split('@', 1)[1]


# ----- IMAP -------------------------------------------------------------------------
def _context():
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class ImapMailbox:
    """Read-only inbox access (EXAMINE). ``imap_class`` is replaceable in tests."""

    def __init__(self, config, password, *, imap_class=imaplib.IMAP4_SSL, folder='INBOX'):
        self.config, self.password, self.imap_class, self.folder = config, password, imap_class, folder
        self.client = None

    def __enter__(self):
        try:
            self.client = self.imap_class(self.config.imap_host, 993, ssl_context=_context(), timeout=TIMEOUT)
        except (OSError, imaplib.IMAP4.error):
            raise MailError('MAIL_NETWORK_UNREACHABLE') from None
        try:
            self.client.login(self.config.address, self.password)
        except imaplib.IMAP4.error:
            self.close()
            raise MailError('MAIL_AUTH_FAILED') from None
        except OSError:
            self.close()
            raise MailError('MAIL_NETWORK_UNREACHABLE') from None
        try:
            kind, _ = self.client.select(self.folder, readonly=True)
        except (imaplib.IMAP4.error, OSError):
            kind = 'NO'
        if kind != 'OK':
            self.close()
            raise MailError('MAIL_PROTOCOL_NOT_ENABLED')
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.client is not None:
            try:
                self.client.logout()
            except Exception:
                pass
            self.client = None

    def uid_validity(self):
        _, data = self.client.response('UIDVALIDITY')
        value = data[0] if data and data[0] is not None else None
        if value is None:
            raise MailError('MAIL_PROTOCOL_ERROR')
        value = int(value.decode() if isinstance(value, bytes) else value)
        if not 0 < value < 2**32:
            raise MailError('MAIL_PROTOCOL_ERROR')
        return value

    def _uids(self, *criteria):
        try:
            kind, data = self.client.uid('SEARCH', None, *criteria)
        except (imaplib.IMAP4.error, OSError):
            raise MailError('MAIL_PROTOCOL_ERROR') from None
        if kind != 'OK':
            raise MailError('MAIL_PROTOCOL_ERROR')
        raw = b' '.join(part for part in data if part)
        return sorted({int(u) for u in raw.split() if u.isdigit()})

    def new_uids(self, last_uid, *, since_days, limit):
        """UIDs after the cursor; on first sync only the recent window, newest ``limit``."""
        if last_uid:
            return [u for u in self._uids('UID', f'{last_uid + 1}:*') if u > last_uid]
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime('%d-%b-%Y')
        return self._uids('SINCE', since)[-limit:]

    def fetch(self, uid):
        try:
            kind, data = self.client.uid('FETCH', str(uid), f'(BODY.PEEK[]<0.{MAX_FETCH}> RFC822.SIZE)')
        except (imaplib.IMAP4.error, OSError):
            raise MailError('MAIL_PROTOCOL_ERROR') from None
        if kind != 'OK':
            raise MailError('MAIL_PROTOCOL_ERROR')
        raw, size = None, None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw = item[1]
                match = re.search(rb'RFC822\.SIZE (\d+)', item[0] or b'')
                size = int(match.group(1)) if match else None
        if raw is None:
            raise MailError('MAIL_MESSAGE_MISSING')
        return raw, (size is not None and size > len(raw))


# ----- parsing ----------------------------------------------------------------------
def _header(message, name):
    value = message.get(name)
    if value is None:
        return ''
    try:
        return re.sub(r'\s+', ' ', str(value)).strip()[:2000]
    except Exception:
        return ''


def _addresses(message, name):
    pairs = getaddresses([_header(message, name)])
    return [dict(name=n.strip()[:200], address=a.lower()) for n, a in pairs if ADDRESS.fullmatch(a or '')][:50]


def _auth(message, from_address):
    """DKIM/SPF results the receiving server recorded, aligned with the From domain."""
    domain = from_address.split('@', 1)[1] if '@' in from_address else ''
    results = ' '.join(str(v) for v in message.get_all('Authentication-Results') or [])
    dkim = any(d.lower() == domain or d.lower().endswith('.' + domain)
               for d in re.findall(r'dkim=pass[^;]*?header\.(?:d|i)=@?([A-Za-z0-9.-]+)', results, re.I))
    spf = any(d.lower() == domain
              for d in re.findall(r'spf=pass[^;]*?smtp\.mailfrom=(?:[^@\s;]*@)?([A-Za-z0-9.-]+)', results, re.I))
    return dict(dkim=dkim, spf=spf, aligned=dkim or spf)


def _text(message):
    plain, html, attachments = [], [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or '').lower()
        kind = part.get_content_type()
        if disposition == 'attachment' or part.get_filename():
            payload = part.get_payload(decode=True) or b''
            attachments.append(dict(name=(part.get_filename() or '')[:200], type=kind, size=len(payload)))
            continue
        if kind not in ('text/plain', 'text/html'):
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b''
            content = payload.decode(part.get_content_charset() or 'utf-8', 'replace')
        (plain if kind == 'text/plain' else html).append(content)
    if plain:
        text = '\n'.join(plain)
    elif html:
        # Scripts, styles and remote resources are dropped: only text survives.
        text = '\n'.join(html_to_text(h)[1] for h in html)
    else:
        text = ''
    text = re.sub(r'\r\n?', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text, attachments[:50]


def parse_message(raw, *, truncated=False):
    """Structured, sanitised view of one RFC 5322 message. Never raises on bad mail."""
    try:
        message = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return dict(parse_error=True, subject='', text='', from_address='', from_name='', to=[], cc=[], reply_to=[],
                    message_id='', in_reply_to='', references='', date=None, attachments=[], auth=dict(dkim=False, spf=False, aligned=False),
                    automated=False, truncated=truncated)
    senders = _addresses(message, 'From')
    sender = senders[0] if senders else dict(name='', address='')
    try:
        date = parsedate_to_datetime(_header(message, 'Date')).timestamp()
    except Exception:
        date = None
    text, attachments = _text(message)
    automated = bool(_header(message, 'Auto-Submitted') and _header(message, 'Auto-Submitted').lower() != 'no'
                     or _header(message, 'Precedence').lower() in ('bulk', 'junk', 'list')
                     or _header(message, 'List-Id') or sender['address'].startswith(('mailer-daemon@', 'postmaster@', 'no-reply@', 'noreply@')))
    return dict(parse_error=False, subject=_header(message, 'Subject')[:500], text=text[:MAX_TEXT],
                text_truncated=truncated or len(text) > MAX_TEXT, from_address=sender['address'], from_name=sender['name'],
                to=_addresses(message, 'To'), cc=_addresses(message, 'Cc'), reply_to=_addresses(message, 'Reply-To'),
                message_id=_header(message, 'Message-ID')[:500], in_reply_to=_header(message, 'In-Reply-To')[:500],
                references=_header(message, 'References')[:4000], date=date, attachments=attachments,
                auth=_auth(message, sender['address']), automated=automated, truncated=truncated)


# ----- SMTP -------------------------------------------------------------------------
class SmtpSender:
    """SMTP over SSL on 465. ``smtp_class`` is replaceable in tests.

    ``connect`` fails before anything is handed over (safe to try again later);
    ``deliver`` distinguishes a definite server answer from a lost connection,
    whose outcome is unknown.
    """

    def __init__(self, config, password, *, smtp_class=smtplib.SMTP_SSL):
        self.config, self.password, self.smtp_class = config, password, smtp_class
        self.client = None

    def connect(self):
        try:
            self.client = self.smtp_class(self.config.smtp_host, 465, timeout=TIMEOUT, context=_context())
            self.client.login(self.config.address, self.password)
        except smtplib.SMTPAuthenticationError:
            self.close()
            raise MailError('MAIL_AUTH_FAILED') from None
        except (smtplib.SMTPException, OSError):
            self.close()
            raise MailError('MAIL_NETWORK_UNREACHABLE') from None
        return self

    def deliver(self, message):
        """Return refused recipients (empty = all accepted). Raise ``MailError`` with
        MAIL_REJECTED (definite) or MAIL_OUTCOME_UNKNOWN."""
        try:
            refused = self.client.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPNotSupportedError):
            raise MailError('MAIL_REJECTED') from None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPException, OSError, socket.timeout):
            raise MailError('MAIL_OUTCOME_UNKNOWN') from None
        return {k: v[0] for k, v in (refused or {}).items()}

    def close(self):
        if self.client is not None:
            try:
                self.client.quit()
            except Exception:
                pass
            self.client = None


def build_reply(config, original, draft, *, message_id):
    message = EmailMessage()
    message['From'] = config.address
    message['To'] = ', '.join(draft['to'])
    if draft['cc']:
        message['Cc'] = ', '.join(draft['cc'])
    message['Subject'] = draft['subject']
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = message_id
    if original and original.get('message_id'):
        message['In-Reply-To'] = original['message_id']
        message['References'] = (original.get('references', '') + ' ' + original['message_id']).strip()[-3900:]
    message['Auto-Submitted'] = 'no'
    message.set_content(draft['body'])
    return message


# ----- storage ----------------------------------------------------------------------
SCHEMA = '''
CREATE TABLE IF NOT EXISTS ms_mail_state (
 tenant TEXT NOT NULL, account TEXT NOT NULL, thread_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, folder TEXT NOT NULL,
 uid_validity INTEGER, last_uid INTEGER NOT NULL, last_sync_at REAL, send_enabled INTEGER NOT NULL, initialized INTEGER NOT NULL,
 updated_at REAL NOT NULL, PRIMARY KEY(tenant,account));
CREATE TABLE IF NOT EXISTS ms_mail_messages (
 tenant TEXT NOT NULL, id TEXT NOT NULL, account TEXT NOT NULL, thread_id TEXT NOT NULL, folder TEXT NOT NULL,
 uid_validity INTEGER NOT NULL, uid INTEGER NOT NULL, header TEXT NOT NULL, safe_text TEXT NOT NULL, status TEXT NOT NULL,
 summary TEXT, draft_state TEXT NOT NULL, received_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,account,folder,uid_validity,uid));
CREATE TABLE IF NOT EXISTS ms_mail_drafts (
 tenant TEXT NOT NULL, id TEXT NOT NULL, mail_id TEXT NOT NULL, thread_id TEXT NOT NULL, version INTEGER NOT NULL,
 recipients TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL,
 approval_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(tenant,id), UNIQUE(tenant,mail_id));
CREATE TABLE IF NOT EXISTS ms_mail_outbox (
 tenant TEXT NOT NULL, id TEXT NOT NULL, draft_id TEXT NOT NULL, version INTEGER NOT NULL, approval_id TEXT NOT NULL,
 rfc_message_id TEXT NOT NULL, status TEXT NOT NULL, error TEXT, refused TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY(tenant,id), UNIQUE(tenant,approval_id));
'''
MAIL_STATUSES = ('new', 'history', 'skipped', 'summarized', 'filtered', 'failed')
OUTBOX_STATES = ('queued', 'sending', 'accepted', 'partial', 'rejected', 'unknown', 'failed')


def _now():
    return time.time()


class MailStore:
    """Mail rows live beside the assistant tables.

    Headers keep real addresses (a reply needs them) but are only ever returned
    to the thread owner; the stored text is redacted. Events carry ids and
    statuses only, so no address or mail text reaches the append-only stream.
    """

    def __init__(self, assistant):
        self.data = assistant
        self.store = assistant.store
        with self.store.connect() as db:
            db.executescript(SCHEMA)

    # state
    def state(self, tenant, account):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_mail_state WHERE tenant=? AND account=?', (tenant, account)).fetchone()
        return dict(row) if row else None

    def save_state(self, tenant, account, **changes):
        current = self.state(tenant, account) or dict(thread_id=None, status='new', error=None, folder='INBOX', uid_validity=None,
                                                      last_uid=0, last_sync_at=None, send_enabled=0, initialized=0)
        current.update(changes)
        with self.store.transaction() as db:
            db.execute('INSERT INTO ms_mail_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant,account) DO UPDATE SET '
                       'thread_id=excluded.thread_id,status=excluded.status,error=excluded.error,folder=excluded.folder,'
                       'uid_validity=excluded.uid_validity,last_uid=excluded.last_uid,last_sync_at=excluded.last_sync_at,'
                       'send_enabled=excluded.send_enabled,initialized=excluded.initialized,updated_at=excluded.updated_at',
                       (tenant, account, current['thread_id'], current['status'], current['error'], current['folder'],
                        current['uid_validity'], current['last_uid'], current['last_sync_at'], int(bool(current['send_enabled'])),
                        int(bool(current['initialized'])), self.store.now(db)))
            if 'status' in changes and current['thread_id']:
                self.data.emit(db, tenant, current['thread_id'], 'mail.state', dict(status=current['status'], error=current['error']))
        return current

    def state_for_thread(self, tenant, thread_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_mail_state WHERE tenant=? AND thread_id=? ORDER BY updated_at DESC LIMIT 1',
                             (tenant, thread_id)).fetchone()
        return dict(row) if row else None

    # messages
    @staticmethod
    def _row(row, *, owner_view=True):
        item = dict(row)
        item['header'] = json.loads(item['header'])
        item['summary'] = json.loads(item['summary']) if item['summary'] else None
        item.pop('tenant', None)
        return item

    def add_message(self, tenant, account, thread_id, *, folder, uid_validity, uid, parsed, status='new'):
        header = {k: parsed[k] for k in ('subject', 'from_address', 'from_name', 'to', 'cc', 'reply_to', 'message_id',
                                          'in_reply_to', 'references', 'date', 'attachments', 'auth', 'automated',
                                          'truncated', 'parse_error')}
        header['subject'] = redact(header['subject'])
        header['from_name'] = redact(header['from_name'])
        text = redact(parsed['text'])
        with self.store.transaction() as db:
            existing = db.execute('SELECT id FROM ms_mail_messages WHERE tenant=? AND account=? AND folder=? AND uid_validity=? AND uid=?',
                                  (tenant, account, folder, uid_validity, uid)).fetchone()
            if existing is None and header['message_id']:
                # After a UIDVALIDITY change the same mail comes back under new UIDs.
                existing = db.execute("SELECT id FROM ms_mail_messages WHERE tenant=? AND account=? AND json_extract(header,'$.message_id')=?",
                                      (tenant, account, header['message_id'])).fetchone()
            if existing is not None:
                return None          # re-synced: never a second record or a second notice
            now = self.store.now(db)
            mail_id = 'mail-' + uuid4().hex[:20]
            db.execute('INSERT INTO ms_mail_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (tenant, mail_id, account, thread_id, folder, uid_validity, uid, canonical(header), text,
                        status, None, 'none', parsed['date'], now, now))
            self.data.emit(db, tenant, thread_id, 'mail', dict(id=mail_id, status=status))
            self.data._audit(db, tenant, mail_id, dict(op='mail.received', account=account, uid_validity=uid_validity, uid=uid,
                                                     automated=header['automated'], auth=header['auth']), 'assistant')
            return mail_id

    def message(self, tenant, mail_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM ms_mail_messages WHERE tenant=? AND id=?', (tenant, mail_id)).fetchone()
        if row is None:
            raise NotFound('mail not found')
        return self._row(row)

    def messages(self, tenant, thread_id, *, limit=50, status=None):
        query, args = 'SELECT * FROM ms_mail_messages WHERE tenant=? AND thread_id=?', [tenant, thread_id]
        if status:
            query += ' AND status=?'
            args.append(status)
        with self.store.connect() as db:
            rows = db.execute(query + ' ORDER BY COALESCE(received_at, created_at) DESC, uid DESC LIMIT ?', (*args, limit)).fetchall()
        return [self._row(r) for r in rows]

    def pending(self, statuses=('new',), draft_states=None):
        with self.store.connect() as db:
            if draft_states:
                rows = db.execute(f"SELECT * FROM ms_mail_messages WHERE draft_state IN ({','.join('?' for _ in draft_states)}) "
                                  'ORDER BY created_at,id', draft_states).fetchall()
            else:
                rows = db.execute(f"SELECT * FROM ms_mail_messages WHERE status IN ({','.join('?' for _ in statuses)}) "
                                  'ORDER BY created_at,id', statuses).fetchall()
        return [dict(self._row(r), tenant=r['tenant']) for r in rows]

    def update_message(self, tenant, mail_id, **changes):
        allowed = {'status', 'summary', 'draft_state'}
        if not set(changes) <= allowed:
            raise InvalidInput('invalid mail update')
        if 'summary' in changes and changes['summary'] is not None:
            changes['summary'] = canonical(changes['summary'])
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_mail_messages WHERE tenant=? AND id=?', (tenant, mail_id)).fetchone()
            if row is None:
                raise NotFound('mail not found')
            sets = ','.join(f'{k}=?' for k in changes)
            db.execute(f'UPDATE ms_mail_messages SET {sets},updated_at=? WHERE tenant=? AND id=?',
                       (*changes.values(), self.store.now(db), tenant, mail_id))
            current = self._row(db.execute('SELECT * FROM ms_mail_messages WHERE tenant=? AND id=?', (tenant, mail_id)).fetchone())
            self.data.emit(db, tenant, row['thread_id'], 'mail', dict(id=mail_id, status=current['status'],
                                                                      draft_state=current['draft_state']))
            return current

    # drafts
    @staticmethod
    def _draft(row):
        item = dict(row)
        item.update(json.loads(item.pop('recipients')))
        item.pop('tenant', None)
        item['placeholders'] = [p for p in PLACEHOLDERS if p in item['body'] or p in item['subject']]
        return item

    def draft(self, tenant, draft_id=None, *, mail_id=None):
        with self.store.connect() as db:
            if draft_id is not None:
                row = db.execute('SELECT * FROM ms_mail_drafts WHERE tenant=? AND id=?', (tenant, draft_id)).fetchone()
            else:
                row = db.execute('SELECT * FROM ms_mail_drafts WHERE tenant=? AND mail_id=?', (tenant, mail_id)).fetchone()
        if row is None:
            raise NotFound('draft not found')
        return self._draft(row)

    def save_draft(self, tenant, mail_id, *, to, cc, subject, body, origin, expected_version=None):
        """Create the draft or write a new version. Any change voids a pending send approval."""
        _recipients(to, cc)
        if type(subject) is not str or not subject.strip() or len(subject) > 500 or type(body) is not str or not body.strip() or len(body) > 50000:
            raise InvalidInput('draft needs a subject (≤500) and a body (≤50000)')
        with self.store.transaction() as db:
            mail = db.execute('SELECT * FROM ms_mail_messages WHERE tenant=? AND id=?', (tenant, mail_id)).fetchone()
            if mail is None:
                raise NotFound('mail not found')
            row = db.execute('SELECT * FROM ms_mail_drafts WHERE tenant=? AND mail_id=?', (tenant, mail_id)).fetchone()
            now = self.store.now(db)
            recipients = canonical(dict(to=list(to), cc=list(cc)))
            if row is None:
                draft_id, version = 'draft-' + uuid4().hex[:20], 1
                db.execute('INSERT INTO ms_mail_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (tenant, draft_id, mail_id, mail['thread_id'], version, recipients, subject, body, 'draft', origin, None, now, now))
            else:
                if expected_version is not None and row['version'] != expected_version:
                    raise Conflict('DRAFT_CHANGED')
                if row['status'] in ('queued', 'sent'):
                    raise Conflict('DRAFT_ALREADY_SENT')
                draft_id, version = row['id'], row['version'] + 1
                if row['approval_id']:
                    db.execute("UPDATE ms_approvals SET status='superseded',decided_at=? WHERE tenant=? AND id=? AND status='pending'",
                               (now, tenant, row['approval_id']))
                db.execute("UPDATE ms_mail_drafts SET version=?,recipients=?,subject=?,body=?,status='draft',origin=?,approval_id=NULL,updated_at=? "
                           'WHERE tenant=? AND id=?', (version, recipients, subject, body, origin, now, tenant, draft_id))
            db.execute("UPDATE ms_mail_messages SET draft_state='ready',updated_at=? WHERE tenant=? AND id=?", (now, tenant, mail_id))
            self.data.emit(db, tenant, mail['thread_id'], 'mail.draft', dict(id=draft_id, mail_id=mail_id, version=version, status='draft'))
            self.data._audit(db, tenant, draft_id, dict(op='mail.draft.saved', mail_id=mail_id, version=version, origin=origin), 'assistant' if origin == 'assistant' else 'person')
        return self.draft(tenant, draft_id)

    def attach_approval(self, tenant, draft_id, approval_id, version):
        with self.store.transaction() as db:
            changed = db.execute("UPDATE ms_mail_drafts SET approval_id=?,status='awaiting_approval' WHERE tenant=? AND id=? AND version=? AND status='draft'",
                                 (approval_id, tenant, draft_id, version)).rowcount
            if not changed:
                raise Conflict('DRAFT_CHANGED')

    def queue_send(self, tenant, approval):
        """Called when a person approves: the exact approved version enters the outbox once."""
        payload = approval['payload']
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_mail_drafts WHERE tenant=? AND id=?', (tenant, payload['draft_id'])).fetchone()
            if row is None or row['version'] != payload['version'] or row['approval_id'] != approval['id']:
                raise Conflict('DRAFT_CHANGED')
            existing = db.execute('SELECT * FROM ms_mail_outbox WHERE tenant=? AND approval_id=?', (tenant, approval['id'])).fetchone()
            if existing is not None:
                return dict(existing)
            now = self.store.now(db)
            account = db.execute('SELECT account FROM ms_mail_messages WHERE tenant=? AND id=?', (tenant, row['mail_id'])).fetchone()[0]
            # Fixed before the first attempt: the Sent folder can be matched against it later.
            message_id = make_msgid(idstring='loom', domain=account.split('@', 1)[1])
            item = dict(id='out-' + uuid4().hex[:20], draft_id=row['id'], version=row['version'], approval_id=approval['id'],
                        rfc_message_id=message_id, status='queued', error=None, refused=None, created_at=now, updated_at=now)
            db.execute('INSERT INTO ms_mail_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?)', (tenant, *item.values()))
            db.execute("UPDATE ms_mail_drafts SET status='queued',updated_at=? WHERE tenant=? AND id=?", (now, tenant, row['id']))
            self.data.emit(db, tenant, row['thread_id'], 'mail.outbox', dict(id=item['id'], draft_id=row['id'], status='queued'))
            self.data._audit(db, tenant, item['id'], dict(op='mail.queued', draft_id=row['id'], version=row['version'],
                                                         approval_id=approval['id']), approval['decided_by'] or 'person')
            return item

    def release_draft(self, tenant, draft_id, approval_id):
        """A rejected send leaves the draft editable again."""
        with self.store.transaction() as db:
            db.execute("UPDATE ms_mail_drafts SET status='draft',approval_id=NULL,updated_at=? WHERE tenant=? AND id=? AND approval_id=?",
                       (self.store.now(db), tenant, draft_id, approval_id))

    def outbox(self, tenant, *, draft_id=None, statuses=None):
        query, args = 'SELECT * FROM ms_mail_outbox WHERE tenant=?', [tenant]
        if draft_id:
            query += ' AND draft_id=?'
            args.append(draft_id)
        if statuses:
            query += f" AND status IN ({','.join('?' for _ in statuses)})"
            args += list(statuses)
        with self.store.connect() as db:
            return [dict(r) for r in db.execute(query + ' ORDER BY created_at,id', args)]

    def queued(self):
        with self.store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM ms_mail_outbox WHERE status='queued' ORDER BY created_at,id")]

    def set_outbox(self, tenant, outbox_id, status, *, error=None, refused=None, expect=None):
        if status not in OUTBOX_STATES:
            raise InvalidInput('invalid outbox status')
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM ms_mail_outbox WHERE tenant=? AND id=?', (tenant, outbox_id)).fetchone()
            if row is None:
                raise NotFound('outbox item not found')
            if expect is not None and row['status'] not in expect:
                raise Conflict('OUTBOX_STATE_CHANGED')
            now = self.store.now(db)
            db.execute('UPDATE ms_mail_outbox SET status=?,error=?,refused=?,updated_at=? WHERE tenant=? AND id=?',
                       (status, error, canonical(refused) if refused else None, now, tenant, outbox_id))
            draft = db.execute('SELECT * FROM ms_mail_drafts WHERE tenant=? AND id=?', (tenant, row['draft_id'])).fetchone()
            draft_status = {'accepted': 'sent', 'partial': 'sent', 'rejected': 'draft', 'failed': 'draft',
                            'unknown': 'unknown', 'sending': 'queued', 'queued': 'queued'}[status]
            if status in ('rejected', 'failed'):
                db.execute("UPDATE ms_mail_drafts SET status=?,approval_id=NULL,updated_at=? WHERE tenant=? AND id=?", (draft_status, now, tenant, draft['id']))
            else:
                db.execute('UPDATE ms_mail_drafts SET status=?,updated_at=? WHERE tenant=? AND id=?', (draft_status, now, tenant, draft['id']))
            self.data.emit(db, tenant, draft['thread_id'], 'mail.outbox', dict(id=outbox_id, draft_id=draft['id'], status=status, error=error))
            self.data._audit(db, tenant, outbox_id, dict(op='mail.outbox', status=status, error=error,
                                                         refused_count=len(refused or {})), 'assistant')


def _recipients(to, cc):
    if (type(to) not in (list, tuple) or not 1 <= len(to) <= 20 or type(cc) not in (list, tuple) or len(cc) > 20
            or any(type(a) is not str or not ADDRESS.fullmatch(a) for a in [*to, *cc])):
        raise InvalidInput('recipients must be 1–20 To and up to 20 Cc valid addresses')


def reply_subject(subject):
    subject = (subject or '').strip()
    return subject if re.match(r'(?i)^(re|回复)\s*[:：]', subject) else ('Re: ' + subject if subject else 'Re:')
