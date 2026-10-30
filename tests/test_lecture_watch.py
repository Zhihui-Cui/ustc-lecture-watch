import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("watch", ROOT / "lecture_watch.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
URL = "https://yjs1.ustc.edu.cn/gsapp/sys/xsbgglappustc/modules/xsbgxk/wxbgbgdz.do"


def row(identity, selectable="1"):
    return {"BGBM": identity, "BGTMZW": "Report " + identity, "SFKXK": selectable}


def payload(rows, total):
    return {"datas": {"wxbgbgdz": {"rows": rows, "totalSize": total}}}


class StateAndListTests(unittest.TestCase):
    def request(self, body="pageNumber=3&pageSize=2&querySetting=%5B%5D"):
        return w.ListRequest(URL, "POST", body, {"content-type": "application/x-www-form-urlencoded",
                                                 "cookie": "SECRET", "x-csrf-token": "TOKEN"})

    def test_first_run_no_old_enrollment_then_new_pending(self):
        state = w.new_state()
        self.assertEqual(w.observe(state, {"old": row("old")}), ([], []))
        fresh, candidates = w.observe(state, {"old": row("old"), "new": row("new", "0")})
        self.assertEqual(fresh, ["new"])
        self.assertEqual(candidates, [])
        self.assertEqual(w.observe(state, {"new": row("new")})[1], ["new"])
        state["reports"]["new"]["status"] = "success"
        self.assertEqual(w.observe(state, {"new": row("new")})[1], [])
        # Reappearing old IDs are never new.
        self.assertEqual(w.observe(state, {"old": row("old")}), ([], []))

    def test_restart_uncertain_and_rejected_not_retried(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "state.json"
            state = w.new_state()
            w.observe(state, {})
            w.observe(state, {"a": row("a"), "b": row("b")})
            state["reports"]["a"]["status"] = "submitting"
            state["reports"]["b"]["status"] = "rejected"
            w.save_state(path, state)
            loaded = w.load_state(path)
            self.assertEqual(loaded["reports"]["a"]["status"], "uncertain")
            self.assertEqual(w.observe(loaded, {"a": row("a"), "b": row("b")})[1], [])

    def test_request_preserves_query_omits_cookies_and_starts_at_one(self):
        spec = self.request().page(1)
        self.assertEqual(parse_qs(spec["body"])["pageNumber"], ["1"])
        self.assertEqual(parse_qs(spec["body"])["querySetting"], ["[]"])
        self.assertNotIn("cookie", spec["headers"])
        self.assertEqual(spec["headers"]["x-csrf-token"], "TOKEN")
        request = w.ListRequest(URL + "?pageNumber=4&pageSize=2", "GET", None, {})
        self.assertEqual(parse_qs(urlsplit(request.page(1)["url"]).query)["pageNumber"], ["1"])
        request = w.ListRequest(URL, "POST", '{"pageNumber":5,"pageSize":2}', {"Content-Type": "application/json"})
        self.assertEqual(w.json.loads(request.page(2)["body"])["pageNumber"], 2)

    def test_paginated_baseline_includes_all_pages(self):
        calls = []
        def fetch(spec):
            number = int(parse_qs(spec["body"])["pageNumber"][0])
            calls.append(number)
            return payload([row("a"), row("b")] if number == 1 else [row("c")], 3)
        records = w.read_all(fetch, self.request())
        self.assertEqual(set(records), {"a", "b", "c"})
        self.assertEqual(calls, [1, 2])

    def test_duplicate_ids_in_single_page_are_merged(self):
        stats = {}
        records = w.read_all(lambda _: payload([row("a"), row("a"), row("b")], 3), self.request(), stats)
        self.assertEqual(set(records), {"a", "b"})
        self.assertEqual(stats["duplicate_rows"], 1)
        state = w.new_state()
        self.assertEqual(w.observe(state, records), ([], []))
        self.assertEqual(w.observe(state, records), ([], []))

    def test_duplicates_at_page_boundary_count_raw_rows(self):
        calls = []
        stats = {}
        def fetch(spec):
            number = int(parse_qs(spec["body"])["pageNumber"][0])
            calls.append(number)
            return payload([row("a"), row("b")] if number == 1 else [row("b"), row("c")], 4)
        records = w.read_all(fetch, self.request(), stats)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(set(records), {"a", "b", "c"})
        self.assertEqual(stats["raw_rows"], 4)

    def test_fully_duplicate_page_requires_matching_page_metadata(self):
        def fetch(spec):
            number = int(parse_qs(spec["body"])["pageNumber"][0])
            result = payload([row("a"), row("b")], 4)
            result["datas"][w.ACTION]["pageNumber"] = number
            return result
        self.assertEqual(set(w.read_all(fetch, self.request())), {"a", "b"})

    def test_server_repeating_wrong_page_metadata_is_rejected(self):
        def fetch(spec):
            result = payload([row("a")], 2)
            result["datas"][w.ACTION]["pageNumber"] = 1
            return result
        with self.assertRaisesRegex(ValueError, "后台返回页码"):
            w.read_all(fetch, self.request())

    def test_query_and_body_page_parameters_updated_together(self):
        request = w.ListRequest(URL + "?pageNumber=1", "POST", "pageNumber=1&pageSize=2", {})
        spec = request.page(3)
        self.assertEqual(parse_qs(urlsplit(spec["url"]).query)["pageNumber"], ["3"])
        self.assertEqual(parse_qs(spec["body"])["pageNumber"], ["3"])

    def test_conflicting_duplicate_eligibility_is_not_submitted(self):
        stats = {}
        records = w.read_all(lambda _: payload([row("a", "1"), row("a", "0")], 2), self.request(), stats)
        state = w.new_state()
        w.observe(state, {})
        self.assertEqual(w.observe(state, records), (["a"], []))
        self.assertEqual(stats["conflicting_reports"], 1)

    def test_partial_pagination_does_not_update_baseline(self):
        state = w.new_state()
        w.observe(state, {"old": row("old")})
        before = copy.deepcopy(state)
        def fetch(spec):
            number = int(parse_qs(spec["body"])["pageNumber"][0])
            return payload([row("new")], 2) if number == 1 else {"code": "LOGIN"}
        with self.assertRaises(ValueError):
            w.read_all(fetch, self.request())
        self.assertEqual(state, before)

    def test_duplicate_and_changed_totals_fail_closed(self):
        with self.assertRaises(ValueError):
            w.read_all(lambda _: payload([row("a")], 2), self.request())
        count = [0]
        def changed(_):
            count[0] += 1
            return payload([row(str(count[0]))], 2 if count[0] == 1 else 3)
        with self.assertRaises(ValueError):
            w.read_all(changed, self.request())
        with self.assertRaises(ValueError):
            w.read_all(lambda _: payload([row("a")], 2), self.request("pageSize=1"))

    def test_invalid_list_and_mutation_urls_rejected(self):
        for bad in ({}, payload([{"BGBM": "a"}], 1), payload([], None), "<html>login</html>"):
            with self.assertRaises(ValueError):
                w.parse_page(bad)
        with self.assertRaises(ValueError):
            w.ListRequest(URL.replace("wxbgbgdz", "selectBg"), "POST", "", {})

    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as directory:
            path = Path(directory) / "state.json"
            with w.single_instance(path):
                with self.assertRaises(RuntimeError):
                    with w.single_instance(path):
                        pass
            with w.single_instance(path):
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
