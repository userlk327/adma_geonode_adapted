"""Move ADMA Studio folders out of the ADAPT team folders, into each person's own private ADMA Studio.

ADMA Studio used to live inside each ADAPT team folder:

    ADAPT/<team>/ADMA Studio/<username>/Reports/...

ADMA now reads ADAPT live from the share and keeps no Folder rows for it, so Studio moved to a
private top-level folder per person:

    ADMA Studio/<team>/Reports/...          (owned by the person, private)

This command moves what the old layout holds. Each person's folder is re-parented under their own
ADMA Studio and renamed to the team's name; nothing is copied or deleted, and files stay where they
are in the media storage (their recorded paths do not change, so they keep working). The moved
folders and files become private. The emptied old "ADMA Studio" folders are then removed, and run
records that named their team by a folder id get the team's name.

Run it before adapt_lazy_cleanup, which refuses to run while ADMA's own folders sit inside ADAPT.

Usage:
    python manage.py askadma_move_studio            # dry run: report only
    python manage.py askadma_move_studio --apply    # make the changes
"""
import json
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from filemanager.models import File, Folder


def _subtree_ids(root_id):
    ids, stack = [], [root_id]
    while stack:
        fid = stack.pop()
        ids.append(fid)
        stack.extend(Folder.objects.filter(parent_id=fid).values_list("id", flat=True))
    return ids


class Command(BaseCommand):
    help = ("Move ADMA Studio folders out of the ADAPT team folders into each person's private ADMA Studio. "
            "Dry run unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Make the changes (default: dry run).")

    def handle(self, *args, **options):
        from filemanager import askadma_studio as S
        apply = options["apply"]
        old_roots = list(Folder.objects.filter(name=S.STUDIO_NAME, third_party_source=S.STUDIO_TAG)
                         .exclude(parent=None).select_related("parent"))
        if not old_roots:
            self.stdout.write("Nothing to move: no ADMA Studio folders inside other folders.")
        moved = removed = 0
        for root in old_roots:
            team = root.parent.name
            people = list(Folder.objects.filter(parent=root).select_related("owner"))
            loose = File.objects.filter(folder=root).count()
            self.stdout.write(f"{team}/ADMA Studio: {len(people)} person folder(s)"
                              + (f", {loose} loose file(s) left in place" if loose else ""))
            for person in people:
                owner = person.owner
                ids = _subtree_ids(person.id)
                n_files = File.objects.filter(folder_id__in=ids).count()
                self.stdout.write(f"  {person.name} -> ADMA Studio/{team} of {owner.username} "
                                  f"({len(ids)} folder(s), {n_files} file(s), made private)")
                if not apply:
                    continue
                with transaction.atomic():
                    target = S.user_studio(owner)
                    existing = Folder.objects.filter(parent=target, owner=owner, name__iexact=team).first()
                    if existing is None:
                        # update(), not save(): no signal moves the media directory, so file paths stay valid
                        Folder.objects.filter(pk=person.pk).update(parent=target, name=team)
                    else:
                        for child in Folder.objects.filter(parent=person):
                            name, n = child.name, 2
                            while Folder.objects.filter(parent=existing, owner=child.owner, name=name).exists():
                                name, n = f"{child.name} ({n})", n + 1
                            Folder.objects.filter(pk=child.pk).update(parent=existing, name=name)
                        File.objects.filter(folder=person).update(folder=existing)
                        Folder.objects.filter(pk=person.pk).delete()
                    Folder.objects.filter(id__in=ids).update(is_public=False)
                    File.objects.filter(folder_id__in=ids).update(is_public=False)
                moved += 1
            if apply and not Folder.objects.filter(parent=root).exists() and not File.objects.filter(folder=root).exists():
                Folder.objects.filter(pk=root.pk).delete()
                removed += 1
        fixed = self._fix_run_records(S, apply)
        verb = "Moved" if apply else "Would move"
        self.stdout.write(f"{verb} {moved if apply else sum(Folder.objects.filter(parent=r).count() for r in old_roots)} "
                          f"person folder(s); {'removed' if apply else 'would remove'} "
                          f"{removed if apply else len(old_roots)} old ADMA Studio folder(s); "
                          f"{'updated' if apply else 'would update'} {fixed} run record(s).")
        if not apply:
            self.stdout.write("Dry run: nothing changed. Run again with --apply.")

    def _fix_run_records(self, S, apply):
        """Run records named their team by a folder id; give them the team's name."""
        n = 0
        if not os.path.isdir(S.RUNS_DIR):
            return 0
        for name in os.listdir(S.RUNS_DIR):
            path = os.path.join(S.RUNS_DIR, name)
            if not name.endswith(".json") or not os.path.isfile(path):
                continue
            try:
                with open(path) as fh:
                    rec = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("team") or not rec.get("team_id"):
                continue
            team = Folder.objects.filter(id=rec["team_id"]).values_list("name", flat=True).first()
            if not team:
                continue
            n += 1
            if apply:
                rec["team"] = team
                tmp = path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(rec, fh)
                os.replace(tmp, path)
        return n
