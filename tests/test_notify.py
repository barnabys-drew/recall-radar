"""The Discord notice must be right about what is new, and must never shout.

Two failures matter more than anything else here. Announcing a recall that was
already announced trains the household to ignore the channel; announcing the
entire corpus because a snapshot went missing does the same thing in one go.
Both are tested directly.

Everything else is Discord's own arithmetic: a payload that exceeds any one of
its ceilings is rejected whole, so a single overlong product description would
silence the notice it belongs to. The formatter is tested against deliberately
absurd input for that reason.
"""

import json
import os
import subprocess
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build  # noqa: E402
from recall_radar import db, notify  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def recall(rid, firm="Acme Foods", product="Cheese, 8 oz", status="Ongoing",
           cls="Class I", reason="Undeclared peanuts", dist="Nationwide",
           date="2026-08-01"):
    return {
        "id": rid, "source": rid.split(":")[0], "recall_number": rid.split(":")[1],
        "event_id": "1", "status": status, "classification": cls, "recalling_firm": firm,
        "product_description": product, "reason": reason, "distribution_pattern": dist,
        "code_info": "", "report_date": date, "initiation_date": date,
        "termination_date": None, "url": "https://example.invalid/" + rid, "raw": {},
    }


def bake(records):
    conn = db.connect(":memory:")
    for r in records:
        db.upsert_recall(conn, r)
    return build.bake(conn)


BASE = [recall(f"fda:F-{i}", firm=f"Firm {i}") for i in range(5)]


class TestReadingAPage(unittest.TestCase):
    def test_payload_survives_a_round_trip_through_the_built_page(self):
        data = bake(BASE)
        page = "<html><script>\nconst DATA = %s;\n</script></html>" % json.dumps(
            data, separators=(",", ":"))
        self.assertEqual(notify.ids(notify.payload_from_page(page)), notify.ids(data))

    def test_a_page_without_a_payload_is_an_error_not_an_empty_diff(self):
        # Silently reading no recalls out of a broken page would look exactly
        # like a quiet day, and the notice would never fire again.
        with self.assertRaises(ValueError):
            notify.payload_from_page("<html>not a build</html>")

    def test_a_corrupt_payload_is_an_error(self):
        with self.assertRaises(ValueError):
            notify.payload_from_page("const DATA = {oops;\n")


class TestWhatIsNew(unittest.TestCase):
    def test_only_recalls_the_last_page_did_not_carry(self):
        data = bake(BASE + [recall("fda:F-99", firm="Newcomer")])
        fresh = notify.new_recalls([r["id"] for r in BASE], data)
        self.assertEqual([r["id"] for r in fresh], ["fda:F-99"])

    def test_nothing_is_new_when_nothing_changed(self):
        data = bake(BASE)
        self.assertEqual(notify.new_recalls(notify.ids(data), data), [])

    def test_a_backdated_recall_still_counts_as_new(self):
        # FDA backfills: a recall published today can carry a date from 2024.
        # Diffing ids rather than dates is the whole reason this is not missed.
        data = bake(BASE + [recall("fda:F-OLD", date="2024-01-05")])
        fresh = notify.new_recalls([r["id"] for r in BASE], data)
        self.assertEqual([r["id"] for r in fresh], ["fda:F-OLD"])

    def test_worst_first_then_newest_then_stable(self):
        records = BASE + [
            recall("fda:N-1", cls="Class III", date="2026-08-20"),
            recall("fda:N-2", cls="Class I", date="2026-08-10"),
            recall("fda:N-3", cls="Class II", date="2026-08-20"),
            recall("fda:N-4", cls="Class I", date="2026-08-20"),
            recall("fda:N-0", cls="Class I", date="2026-08-20"),
        ]
        data = bake(records)
        fresh = notify.new_recalls([r["id"] for r in BASE], data)
        self.assertEqual([r["id"] for r in fresh],
                         ["fda:N-0", "fda:N-4", "fda:N-2", "fda:N-3", "fda:N-1"])

    def test_ordering_does_not_depend_on_the_order_recalls_arrived(self):
        extra = [recall("fda:N-%d" % i, cls="Class I", date="2026-08-20") for i in range(6)]
        forward = notify.new_recalls([r["id"] for r in BASE], bake(BASE + extra))
        backward = notify.new_recalls([r["id"] for r in BASE], bake(BASE + extra[::-1]))
        self.assertEqual([r["id"] for r in forward], [r["id"] for r in backward])


class TestFormatting(unittest.TestCase):
    def test_states_read_like_a_person_wrote_them(self):
        self.assertEqual(notify.where(["US", "CA"]), "Nationwide")
        self.assertEqual(notify.where(["WI", "IL"]), "IL, WI")
        self.assertEqual(notify.where([]), "")
        long = notify.where(["S%02d" % i for i in range(12)])
        self.assertTrue(long.endswith("+4 more"), long)

    def test_clip_collapses_whitespace_and_marks_the_cut(self):
        self.assertEqual(notify.clip("a\n\n  b", 80), "a b")
        self.assertTrue(notify.clip("x" * 100, 10).endswith("…"))
        self.assertLessEqual(len(notify.clip("x" * 100, 10)), 10)

    def test_no_message_when_nothing_is_new(self):
        self.assertEqual(notify.messages([]), [])

    def test_every_discord_ceiling_holds_on_absurd_input(self):
        # A real product_description runs to a thousand characters of lot codes,
        # and a distribution_pattern can list every state twice.
        monstrous = [recall(f"fda:B-{i}", firm="F" * 400, product="P" * 5000,
                            reason="R" * 3000, dist=", ".join(["IL, WI, CA"] * 40))
                     for i in range(60)]
        data = bake(monstrous)
        fresh = notify.new_recalls([], data)
        msgs = notify.messages(fresh, app_url="https://example.invalid/", limit=40)

        self.assertTrue(msgs)
        for m in msgs:
            self.assertLessEqual(len(m["embeds"]), notify.MAX_EMBEDS_PER_MESSAGE)
            self.assertLessEqual(sum(notify._embed_chars(e) for e in m["embeds"]),
                                 notify.MAX_EMBED_CHARS)
            self.assertLessEqual(len(m.get("content", "")), notify.MAX_CONTENT)
            for e in m["embeds"]:
                self.assertLessEqual(len(e["title"]), notify.MAX_TITLE)
                self.assertLessEqual(len(e.get("description", "")), notify.MAX_DESCRIPTION)
                self.assertLessEqual(len(e["footer"]["text"]), notify.MAX_FOOTER)
                for f in e["fields"]:
                    self.assertLessEqual(len(f["value"]), notify.MAX_FIELD_VALUE)

    def test_one_card_can_never_blow_the_whole_message_budget(self):
        # Discord counts every embed in a message against one 6000-character
        # budget, so a card that is oversize on its own can never be batched
        # into a valid message at all - it would silence the notice it is in.
        data = bake([recall("fda:B-1", firm="F" * 400, product="P" * 9000,
                            reason="R" * 9000, dist=", ".join(["IL, WI"] * 60))])
        msgs = notify.messages(notify.new_recalls([], data))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(len(msgs[0]["embeds"]), 1)
        self.assertLess(notify._embed_chars(msgs[0]["embeds"][0]), notify.MAX_EMBED_CHARS)

    def test_the_headline_counts_the_whole_batch_not_the_visible_part(self):
        records = ([recall(f"fda:C-{i}", cls="Class I") for i in range(45)]
                   + [recall("fda:D-1", cls="Class III")])
        fresh = notify.new_recalls([], bake(records))
        content = notify.messages(fresh, limit=10)[0]["content"]
        self.assertIn("46 new recalls", content)
        self.assertIn("45 Class I", content)
        self.assertIn("36 more are not", content)

    def test_the_notice_cannot_ping_the_channel(self):
        data = bake([recall("fda:B-1", firm="@everyone Foods")])
        for m in notify.messages(notify.new_recalls([], data)):
            self.assertEqual(m["allowed_mentions"], {"parse": []})


class TestSending(unittest.TestCase):
    def _http_error(self, code, retry_after=None):
        body = json.dumps({"retry_after": retry_after}).encode() if retry_after else b"{}"
        return urllib.error.HTTPError(
            "https://discord.invalid/hook", code, "nope",
            {"Retry-After": str(retry_after or 0)}, __import__("io").BytesIO(body))

    def test_a_rate_limit_is_waited_out_and_the_message_still_goes(self):
        calls, slept = [], []
        err = self._http_error(429, retry_after=0.75)

        def post(url, payload, timeout):
            calls.append(payload)
            if len(calls) == 1:
                raise err
            return 204, b""

        notify.send("https://discord.invalid/hook", [{"content": "x"}],
                    sleep=slept.append, post=post)
        self.assertEqual(len(calls), 2)
        self.assertEqual(slept, [0.75])

    def test_a_bad_webhook_fails_loudly_without_leaking_the_url(self):
        secret = "https://discord.com/api/webhooks/123/SUPER-SECRET-TOKEN"

        def post(url, payload, timeout):
            raise self._http_error(404)

        with self.assertRaises(notify.DiscordError) as caught:
            notify.send(secret, [{"content": "x"}], sleep=lambda s: None, post=post)
        self.assertNotIn("SUPER-SECRET-TOKEN", str(caught.exception))
        self.assertIn("404", str(caught.exception))

    def test_a_client_error_is_not_retried(self):
        calls = []

        def post(url, payload, timeout):
            calls.append(payload)
            raise self._http_error(400)

        with self.assertRaises(notify.DiscordError):
            notify.send("https://discord.invalid/hook", [{"content": "x"}],
                        sleep=lambda s: None, post=post)
        self.assertEqual(len(calls), 1)

    def test_messages_are_paced_so_the_webhook_is_not_flooded(self):
        slept = []
        notify.send("https://discord.invalid/hook", [{"content": str(i)} for i in range(3)],
                    sleep=slept.append, post=lambda u, p, t: (204, b""))
        self.assertEqual(len(slept), 2)


class TestTheCommandLine(unittest.TestCase):
    """The CLI is the part CI runs, so its refusals are tested for real."""

    def run_notify(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(ROOT, "notify.py"), *args],
                              capture_output=True, text=True, cwd=ROOT,
                              env={**os.environ, **(env or {})})

    def test_a_missing_snapshot_announces_nothing(self):
        # The loud failure this guards: with no ledger, every open recall in the
        # corpus looks new, and the channel gets nine hundred cards.
        out = self.run_notify("post", "--before", "/nonexistent/before.json", "--dry-run")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing to announce", out.stdout)
        self.assertNotIn("embeds", out.stdout)

    def test_an_empty_snapshot_announces_nothing(self):
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "rr-empty-snapshot.json")
        with open(path, "w") as fh:
            json.dump({"built": None, "ids": []}, fh)
        try:
            out = self.run_notify("post", "--before", path, "--dry-run")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertNotIn("embeds", out.stdout)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
