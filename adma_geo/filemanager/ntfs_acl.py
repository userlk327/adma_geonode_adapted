"""The snr18 permission export, and what Windows makes of it.

snr18 exports the ACL of every ADAPT folder that has entries of its own or
blocks inheritance, one row per entry:

    "Path","Protected","Identity","Rights","Type","AppliesTo"
    "Flux Measurements","True","NEAD\\12345678","FullControl","Allow","ContainerInherit, ObjectInherit/None"

Path is relative to the ADAPT root, "(root)" is the root itself and "\\"
separates folders. Protected=True means the folder does not inherit from its
parent. AppliesTo is the inheritance flags and the propagation flags, joined
by "/". An optional IsInherited column marks entries a folder only inherited;
they are dropped, since the folder they come from carries them explicitly.

Everything here is plain Python over the export. adapt_acl check uses it to
work out what snr18 allows independently of filemanager.permissions, so it
must never read FolderGrant or the permissions module.
"""
import csv
from collections.abc import Iterable
from dataclasses import dataclass

COLUMNS = ('Path', 'Protected', 'Identity', 'Rights', 'Type', 'AppliesTo')
ROOT = '(root)'

# System.Security.AccessControl.FileSystemRights, by the names Get-Acl prints.
# Several names share a bit: the first is the file meaning, the second the folder one.
FILE_SYSTEM_RIGHTS = {
    'ReadData': 0x1, 'ListDirectory': 0x1,
    'WriteData': 0x2, 'CreateFiles': 0x2,
    'AppendData': 0x4, 'CreateDirectories': 0x4,
    'ReadExtendedAttributes': 0x8,
    'WriteExtendedAttributes': 0x10,
    'ExecuteFile': 0x20, 'Traverse': 0x20,
    'DeleteSubdirectoriesAndFiles': 0x40,
    'ReadAttributes': 0x80,
    'WriteAttributes': 0x100,
    'Delete': 0x10000,
    'ReadPermissions': 0x20000,
    'ChangePermissions': 0x40000,
    'TakeOwnership': 0x80000,
    'Synchronize': 0x100000,
    'Read': 0x20089,
    'ReadAndExecute': 0x200A9,
    'Write': 0x116,
    'Modify': 0x301BF,
    'FullControl': 0x1F01FF,
}
RIGHT_NAMES = {name.lower(): mask for name, mask in FILE_SYSTEM_RIGHTS.items()}

# Get-Acl prints a mask it has no name for as a number, a negative one when the
# top bit is set. Those are mostly generic rights, which Windows maps to file rights.
GENERIC_RIGHTS = {
    0x80000000: 0x120089,    # GENERIC_READ: FILE_GENERIC_READ
    0x40000000: 0x120116,    # GENERIC_WRITE: FILE_GENERIC_WRITE
    0x20000000: 0x1200A0,    # GENERIC_EXECUTE: FILE_GENERIC_EXECUTE
    0x10000000: 0x1F01FF,    # GENERIC_ALL: FILE_ALL_ACCESS
}

# What ADMA's two levels need, as FileSystemRights bits.
#
# read: ReadData (ListDirectory), so the folder can be listed and its files read.
# Read and ReadAndExecute both have it. Traverse is not required: Windows gives
# every account "Bypass traverse checking" by default.
#
# write: read, plus WriteData and AppendData (create and change files and
# folders), plus Delete or DeleteSubdirectoriesAndFiles. That is what Modify and
# FullControl hold, and an ADMA write grant does not tell changing apart from
# deleting. Write without a delete right (for example "Write, ReadAndExecute")
# therefore stays read: ADMA has no level that creates and changes but never
# deletes, and giving less than snr18 is the safe side. import lists those entries.
READ_BITS = 0x1
WRITE_BITS = 0x2 | 0x4
DELETE_BITS = 0x10000 | 0x40

# Built-in groups that on a domain server hold every signed-in domain account.
EVERY_DOMAIN_USER = frozenset({'builtin\\users', 'everyone', 'nt authority\\authenticated users'})


class ExportError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    line: int
    path: tuple[str, ...]
    protected: bool
    identity: str
    rights_text: str
    allow: bool
    applies_to: str

    @property
    def mask(self):
        """The rights as a FileSystemRights mask, generic bits mapped to file rights. None if unreadable."""
        return rights_mask(self.rights_text)

    @property
    def reads(self):
        mask = self.mask
        return mask is not None and mask & READ_BITS == READ_BITS

    @property
    def writes(self):
        mask = self.mask
        return self.reads and mask & WRITE_BITS == WRITE_BITS and bool(mask & DELETE_BITS)

    @property
    def writes_without_delete(self):
        """Read and write data but no delete right: snr18 gives more than read, ADMA can only give read."""
        mask = self.mask
        return self.reads and not self.writes and mask & WRITE_BITS == WRITE_BITS

    @property
    def _flags(self):
        inherit, _, propagate = self.applies_to.partition('/')
        return ({f.strip() for f in inherit.split(',')}, {f.strip() for f in propagate.split(',')})

    @property
    def container_inherit(self):
        """Whether subfolders inherit the entry."""
        return 'ContainerInherit' in self._flags[0]

    @property
    def object_inherit(self):
        return 'ObjectInherit' in self._flags[0]

    @property
    def no_propagate(self):
        """Inherited by direct children only, not further down."""
        return 'NoPropagateInherit' in self._flags[1]

    @property
    def inherit_only(self):
        """Applies to what is beneath the folder, not to the folder itself."""
        return 'InheritOnly' in self._flags[1]

    @property
    def whole_subtree(self):
        """Whether the entry reaches the folder, its files and every folder beneath, as an ADMA grant does."""
        return self.container_inherit and self.object_inherit and not self.no_propagate and not self.inherit_only

    @property
    def display_path(self):
        return path_text(self.path)

    def key(self):
        return self.identity.lower()


def rights_mask(text):
    """FileSystemRights mask for a Rights value such as "Modify, Synchronize" or "-1610612736".

    Returns None when any part is neither a known name nor a 32 bit number.
    """
    mask = 0
    parts = [part.strip() for part in (text or '').split(',')]
    if not any(parts):
        return None
    for part in parts:
        if part.lower() in RIGHT_NAMES:
            mask |= RIGHT_NAMES[part.lower()]
            continue
        try:
            number = int(part)
        except ValueError:
            return None
        if not -2 ** 31 <= number < 2 ** 32:
            return None
        mask |= number & 0xFFFFFFFF
    for generic, specific in GENERIC_RIGHTS.items():
        if mask & generic:
            mask = (mask & ~generic) | specific
    return mask


def split_path(text) -> tuple[str, ...]:
    text = text.strip()
    if text == ROOT:
        return ()
    return tuple(part for part in text.split('\\') if part)


def path_key(path) -> tuple[str, ...]:
    """A path for lookups: NTFS compares names without case, so "Soils" and "SOILS" are one folder."""
    return tuple(part.lower() for part in path)


def path_text(path) -> str:
    return '\\'.join(path) if path else ROOT


def _true(value) -> bool:
    return (value or '').strip().lower() == 'true'


def read_export(path) -> list[Entry]:
    """The explicit entries of the export at `path`. Raises ExportError if it cannot be read."""
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            fields = [name.strip() for name in reader.fieldnames or []]
            missing = [c for c in COLUMNS if c not in fields]
            if missing:
                raise ExportError(f'{path} is missing the column{"s" if len(missing) > 1 else ""} '
                                  f'{", ".join(missing)}. Expected {", ".join(COLUMNS)}.')
            reader.fieldnames = fields
            entries = []
            for rec in reader:
                if _true(rec.get('IsInherited')):
                    continue
                entries.append(Entry(
                    line=reader.line_num,
                    path=split_path(rec['Path'] or ''),
                    protected=_true(rec['Protected']),
                    identity=(rec['Identity'] or '').strip(),
                    rights_text=(rec['Rights'] or '').strip(),
                    allow=(rec['Type'] or '').strip().lower() == 'allow',
                    applies_to=(rec['AppliesTo'] or '').strip(),
                ))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise ExportError(f'Cannot read {path}: {exc}')
    return entries


class Acl:
    """Windows inheritance over the exported entries.

    A folder's effective entries are its own plus those it inherits: walking up
    from the folder, each parent passes down the entries that reach subfolders
    (ContainerInherit), and NoPropagateInherit entries only to the parent's direct
    children. The walk stops at a folder that blocks inheritance (Protected).
    Folders the export does not list have no entries of their own and inherit.
    Paths compare without case, as on NTFS.
    Only Allow entries count: adapt_acl refuses an export with Deny entries.
    """

    def __init__(self, entries: Iterable[Entry]):
        self.explicit = {}               # path_key -> entries
        self.protected = set()           # path_keys
        for e in entries:
            self.explicit.setdefault(path_key(e.path), []).append(e)
            if e.protected:
                self.protected.add(path_key(e.path))

    def effective(self, path) -> list[Entry]:
        path = path_key(path)
        found = [e for e in self.explicit.get(path, ()) if not e.inherit_only]
        child, distance = path, 1
        while child and child not in self.protected:
            parent = child[:-1]
            found += [e for e in self.explicit.get(parent, ())
                      if e.container_inherit and not (e.no_propagate and distance > 1)]
            child, distance = parent, distance + 1
        return found

    def matching(self, path, identities) -> list[Entry]:
        """Effective entries on `path` for anyone in `identities` (compared without case)."""
        wanted = {i.lower() for i in identities}
        return [e for e in self.effective(path) if e.allow and e.key() in wanted]

    def access(self, path, identities) -> tuple[bool, bool]:
        """(read, write) on the folder itself for someone holding `identities`."""
        entries = self.matching(path, identities)
        write = any(e.writes for e in entries)
        return write or any(e.reads for e in entries), write
