"""apply-agent command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .answer_bank import AnswerBank
from .ats import detect
from .profile import Profile

TERMS_REMINDER = (
    "Reminder: some employers' terms prohibit automated access. This tool fills forms for you "
    "to review; you read every page and click Submit yourself."
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="apply-agent")
    ap.add_argument("--profile", default="profile.json")
    ap.add_argument("--bank", default="answer_bank.json")
    ap.add_argument("--data", default="data", help="runs, logs, Gmail token")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="fill an application up to the Review page")
    a.add_argument("url")
    a.add_argument("--company", default="", help="company name (defaults to the tenant slug)")
    a.add_argument("--cdp", help="attach to your running Chrome, e.g. http://localhost:9222")
    a.add_argument("--profile-dir", help="persistent Chrome profile dir (if not using --cdp)")
    a.add_argument("--force", action="store_true", help="ignore the targeting filter")
    a.add_argument("--no-llm", action="store_true", help="never call a model; unknowns go to you")
    a.add_argument("--no-gmail", action="store_true", help="click verification links yourself")
    a.add_argument("--speed", type=float, default=1.0, help=">1 is faster than human pace")

    sub.add_parser("detect", help="show which ATS a URL belongs to").add_argument("url")
    sub.add_parser("check-profile", help="validate profile.json")

    b = sub.add_parser("bank", help="inspect or edit the answer bank")
    bsub = b.add_subparsers(dest="bank_cmd", required=True)
    bsub.add_parser("list")
    bsub.add_parser("pending")
    ba = bsub.add_parser("approve")
    ba.add_argument("key", help="normalized question key, or 'all'")
    ba.add_argument("--answer")
    bd = bsub.add_parser("add")
    bd.add_argument("question")
    bd.add_argument("answer")
    bd.add_argument("--kind", default="choice", choices=["text", "essay", "choice", "multichoice", "checkbox", "date"])
    bt = bsub.add_parser("test", help="show how a question would resolve, without a model")
    bt.add_argument("question")

    sub.add_parser("log", help="list logged applications")
    args = ap.parse_args(argv)

    if args.cmd == "detect":
        print(detect(args.url))
        return 0
    if args.cmd == "check-profile":
        p = Profile.load(args.profile)
        print(f"OK: {p.contact.first_name} {p.contact.last_name}, {len(p.work_history)} jobs, "
              f"{len(p.education)} schools, {p.years_experience()} years experience")
        if not Path(p.resume_path).expanduser().exists():
            print(f"WARNING: resume not found at {p.resume_path}")
        return 0
    if args.cmd == "bank":
        return _bank(args)
    if args.cmd == "log":
        log = Path(args.data) / "applications.jsonl"
        for line in log.read_text().splitlines() if log.exists() else []:
            r = json.loads(line)
            print(f"{r['started_at']}  {r['status']:<10} {r['company']:<20} {r['job_title'][:50]}  {r['run_dir']}")
        return 0
    if args.cmd == "apply":
        return _apply(args)
    return 1


def _bank(args) -> int:
    bank = AnswerBank(args.bank)
    if args.bank_cmd in ("list", "pending"):
        items = bank.pending() if args.bank_cmd == "pending" else bank.entries
        for k, e in items.items():
            flag = " " if e.approved else "?"
            print(f"{flag} [{e.kind}/{e.source}] {k}\n      -> {e.answer[:200]}")
        return 0
    if args.bank_cmd == "approve":
        keys = list(bank.pending()) if args.key == "all" else [args.key]
        for k in keys:
            bank.approve(k, args.answer)
        bank.save()
        print(f"approved {len(keys)}")
        return 0
    if args.bank_cmd == "add":
        bank.add(args.question, args.answer, kind=args.kind, approved=True)
        bank.save()
        return 0
    if args.bank_cmd == "test":
        m = bank.lookup(args.question)
        print(f"{m.tier} ({m.score:.0f}) -> {m.key!r}: {m.entry.answer}" if m else "no match: would go to a model")
        return 0
    return 1


def _apply(args) -> int:
    from .accounts.vault import KeyringVault
    from .browser import open_page
    from .pacing import Pacer
    from .resolver import Resolver

    target = detect(args.url)
    if target.ats != "workday":
        print(f"No driver for {target.ats} yet (Workday only for now).", file=sys.stderr)
        return 2
    print(TERMS_REMINDER)
    profile = Profile.load(args.profile)
    bank = AnswerBank(args.bank)
    llm = None
    if not args.no_llm:
        from .llm import Llm
        llm = Llm()
    resolver = Resolver(profile, bank, llm, company=args.company or target.tenant)
    gmail = None
    if not args.no_gmail:
        from .accounts.gmail import GmailPoller
        gmail = GmailPoller(args.data)
    pacer = Pacer(speed=args.speed)

    from .drivers.workday.driver import DriverOptions, WorkdayDriver

    with open_page(cdp_url=args.cdp, profile_dir=args.profile_dir) as page:
        fallback = None
        if llm is not None:
            from .fallback import run_fallback
            fallback = lambda pg: run_fallback(pg, profile, pacer, job_description=resolver.job_description,
                                               client=llm.client)
        driver = WorkdayDriver(
            page=page, target=target, profile=profile, bank=bank, resolver=resolver,
            vault=KeyringVault(), pacer=pacer, gmail=gmail, fallback=fallback,
            options=DriverOptions(data_dir=Path(args.data), company=args.company, force=args.force),
        )
        record = driver.run()
    print(f"status: {record.status}  ({record.run_dir})")
    return 0 if record.status in ("submitted", "at_review", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
