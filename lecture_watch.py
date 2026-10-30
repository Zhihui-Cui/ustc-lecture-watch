"""每分钟检查新增学术报告，使用自己的浏览器登录状态提交选课。"""

import argparse
import contextlib
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PORTAL = "https://yjs1.ustc.edu.cn/gsapp/sys/yjsemaphome/portal/index.do?forceCas=1"
ACTION = "wxbgbgdz"
INTERVAL = 60
STATE_PATH = Path(__file__).resolve().with_name("lecture_watch_state.json")


def log(message):
    print(time.strftime("[%H:%M:%S] ") + message, flush=True)


FETCH_JS = r"""async spec => {
    if (new URL(spec.url).origin !== location.origin)
        throw new Error('列表请求与当前页面不在同一域名，请重新登录并进入讲座列表。');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
        const response = await fetch(spec.url, {
            method: spec.method, headers: spec.headers,
            body: spec.method === 'GET' ? undefined : spec.body,
            credentials: 'include', cache: 'no-store', signal: controller.signal
        });
        if (!response.ok) throw new Error('列表 HTTP 状态：' + response.status);
        if (response.redirected && new URL(response.url).origin !== location.origin)
            throw new Error('登录状态已失效，请在浏览器中重新登录。');
        const text = await response.text();
        try { return JSON.parse(text); }
        catch (_) { throw new Error('列表未返回 JSON，可能需要重新登录。'); }
    } finally { clearTimeout(timer); }
}"""


SUBMIT_JS = r"""args => new Promise(resolve => {
    let sent = false, done = false;
    const finish = result => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        resolve(result);
    };
    const timer = setTimeout(() => finish({
        status: sent ? 'uncertain' : 'not_sent',
        message: sent ? '等待后台结果超时，请手动检查已选报告。' : '选课模块加载超时。'
    }), args.timeout_ms || 20000);
    if (typeof require !== 'function' || typeof XGJSUTIL === 'undefined') {
        finish({status: 'not_sent', message: '未找到页面选课模块。'});
        return;
    }
    try {
        require(['modules/xsbgxk/xsbgxkBS'], bs => {
            if (done) return;
            if (!bs.api || !bs.api.selectBg) {
                finish({status: 'not_sent', message: '未找到选课接口。'});
                return;
            }
            try {
                sent = true;
                XGJSUTIL.postAsyncCustom(bs.api.selectBg, {BGBM: args.bgbm}, data => {
                    if (data && typeof data === 'object' && data.code != null) {
                        finish({status: String(data.code) === '0' ? 'success' : 'rejected',
                            code: String(data.code), message: String(data.msg || '')});
                    } else {
                        finish({status: 'uncertain', message: '后台返回格式未知，请检查已选报告。'});
                    }
                });
            } catch (_) {
                finish({status: 'uncertain', message: '提交期间出现异常，请检查已选报告。'});
            }
        }, () => finish({status: 'not_sent', message: '选课模块加载失败。'}));
    } catch (_) { finish({status: 'not_sent', message: '无法加载页面选课模块。'}); }
})"""


class ListRequest:
    """仅复用浏览器实际发出的未选报告查询；不猜测接口地址。"""

    def __init__(self, url, method, body, headers):
        parts = urlsplit(url)
        if (parts.hostname not in {"yjs1.ustc.edu.cn", "yjs2.ustc.edu.cn"}
                or "/xsbgglappustc/" not in parts.path
                or not parts.path.endswith("/wxbgbgdz.do")):
            raise ValueError("不是已确认的未选报告查询接口。")
        self.url = url
        self.method = method.upper()
        if self.method not in {"GET", "POST"}:
            raise ValueError("不支持此列表查询方式。")
        self.headers = {k: v for k, v in headers.items()
                        if k.lower() in {"content-type", "accept", "x-requested-with"}
                        or "csrf" in k.lower() or "xsrf" in k.lower()}
        content_type = next((v for k, v in headers.items()
                             if k.lower() == "content-type"), "")
        self.json_body = "application/json" in content_type.lower()
        self.query = parse_qsl(parts.query, keep_blank_values=True)
        if self.json_body:
            self.data = json.loads(body or "{}")
            if not isinstance(self.data, dict):
                raise ValueError("列表请求 JSON 不是对象。")
        else:
            self.data = parse_qsl(body or "", keep_blank_values=True)
        self.page_location = None
        for location, pairs in (("query", self.query), ("body", self.data)):
            keys = pairs.keys() if isinstance(pairs, dict) else [k for k, _ in pairs]
            for key in keys:
                if key.lower() == "pagenumber":
                    self.page_location = (location, key)

    def page(self, number):
        query = list(self.query)
        data = dict(self.data) if isinstance(self.data, dict) else list(self.data)
        if self.page_location:
            # 有些查询同时在 URL 和请求体中带页码，两处必须一起更新。
            query = [(k, str(number) if k.lower() == "pagenumber" else v)
                     for k, v in query]
            if isinstance(data, dict):
                data = {k: number if k.lower() == "pagenumber" else v
                        for k, v in data.items()}
            else:
                data = [(k, str(number) if k.lower() == "pagenumber" else v)
                        for k, v in data]
        parts = urlsplit(self.url)
        return {"url": urlunsplit((parts.scheme, parts.netloc, parts.path,
                                   urlencode(query), "")),
                "method": self.method, "headers": self.headers,
                "body": json.dumps(data, ensure_ascii=False) if self.json_body
                else urlencode(data)}


def page_table(payload):
    if not isinstance(payload, dict):
        raise ValueError("列表响应格式未知；本轮不提交。")
    table = payload.get("datas", {}).get(ACTION) if isinstance(payload.get("datas"), dict) else None
    if table is None:
        table = payload.get(ACTION)
    if table is None and "rows" in payload:
        table = payload
    if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
        raise ValueError("未找到 wxbgbgdz.rows；可能登录失效或接口格式已变化。")
    return table


def parse_page(payload):
    table = page_table(payload)
    raw_total = table.get("totalSize")
    if isinstance(raw_total, bool) or not str(raw_total).isdigit():
        raise ValueError("列表缺少有效 totalSize，无法确认是否已读取全部分页。")
    total = int(raw_total)
    rows = table["rows"]
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("BGBM") or "").strip():
            raise ValueError("报告记录缺少 BGBM；本轮不提交。")
        if "SFKXK" not in row:
            raise ValueError("报告记录缺少 SFKXK，无法确认是否允许选课。")
    return rows, total


def read_all(fetch_page, request, stats=None):
    records = {}
    expected = None
    raw_count = 0
    signatures = set()
    conflicting = set()
    for number in range(1, 501):
        payload = fetch_page(request.page(number))
        rows, total = parse_page(payload)
        table = page_table(payload)
        reported_page = table.get("pageNumber")
        if reported_page is not None:
            if not str(reported_page).isdigit() or int(reported_page) != number:
                raise ValueError(f"请求第 {number} 页，后台返回页码 {reported_page!r}，分页参数未生效；本轮不提交。")
        if expected is None:
            expected = total
        if total != expected:
            raise ValueError("翻页时列表数量发生变化，下轮重新读取。")
        raw_count += len(rows)
        if raw_count > total:
            raise ValueError(f"第 {number} 页：累计返回 {raw_count} 行，超过总数 {total}，分页结果不一致。")
        signature = tuple(str(row["BGBM"]).strip() for row in rows)
        if rows and signature in signatures and reported_page is None:
            raise ValueError(f"第 {number} 页与之前某页完全相同，且没有后台页码可核实；可能反复返回同一页。")
        signatures.add(signature)
        before = len(records)
        for row in rows:
            identity = str(row["BGBM"]).strip()
            if identity in records:
                if str(records[identity]["SFKXK"]) != str(row["SFKXK"]):
                    # 重复记录的资格标志矛盾时，等待下轮一致的数据。
                    conflicting.add(identity)
                    records[identity]["SFKXK"] = "0"
                continue
            records[identity] = dict(row)
        if number > 1 and rows and len(records) == before and reported_page is None:
            raise ValueError(f"第 {number} 页全部是之前的编号，且没有后台页码可核实；本轮不提交。")
        if stats is not None:
            stats.update(pages=number, raw_rows=raw_count, unique_reports=len(records),
                         duplicate_rows=raw_count - len(records), conflicting_reports=len(conflicting))
        # totalSize 是后台记录行数；重复 BGBM 合并后，讲座数可能小于行数。
        if raw_count == expected:
            return records
        if not rows:
            raise ValueError(f"第 {number} 页为空，但只读到 {raw_count}/{expected} 行，下轮重新读取。")
        if not request.page_location:
            raise ValueError("查询没有 pageNumber 参数，无法安全检查全部分页。")
    raise ValueError("列表超过 500 页，已停止本轮查询。")


def new_state():
    return {"version": 1, "initialized": False, "seen": [], "reports": {}}


def load_state(path):
    if not path.exists():
        return new_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("version") != 1
            or not isinstance(data.get("initialized"), bool)
            or not isinstance(data.get("seen"), list)
            or not all(isinstance(item, str) for item in data["seen"])
            or not isinstance(data.get("reports"), dict)):
        raise ValueError("状态文件格式错误。为避免误选旧报告，程序已停止。")
    for report in data["reports"].values():
        if not isinstance(report, dict) or report.get("status") not in {
                "pending", "submitting", "success", "rejected", "uncertain"}:
            raise ValueError("状态记录无效，程序已停止。")
        if report["status"] == "submitting":
            report["status"] = "uncertain"
            report["message"] = "上次提交期间中断，请检查已选报告；不自动重复提交。"
    return data


def save_state(path, state):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(state, output, ensure_ascii=False, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def observe(state, records):
    if not state["initialized"]:
        state["seen"] = sorted(records)
        state["initialized"] = True
        return [], []
    fresh = sorted(set(records) - set(state["seen"]))
    for identity in fresh:
        state["reports"][identity] = {
            "title": str(records[identity].get("BGTMZW") or identity),
            "status": "pending", "detected_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    state["seen"] = sorted(set(state["seen"]) | set(records))
    candidates = [identity for identity, report in state["reports"].items()
                  if report["status"] == "pending" and identity in records
                  and str(records[identity]["SFKXK"]) == "1"]
    return fresh, candidates


@contextlib.contextmanager
def single_instance(path):
    handle = path.with_suffix(".lock").open("a+b")
    try:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("已有一个监控程序在运行，请勿重复启动。") from exc
        yield
    finally:
        handle.close()


def find_frame(context):
    for tab in context.pages:
        if tab.is_closed():
            continue
        for frame in tab.frames:
            if "/xsbgglappustc/" not in urlsplit(frame.url).path:
                continue
            try:
                if frame.locator("#wxbg-index-table").count():
                    return frame
            except Exception:
                continue
    raise RuntimeError("未找到未选报告列表，请在浏览器中登录并打开“学术报告选课”。")


def pump_wait(context, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        pages = [page for page in context.pages if not page.is_closed()]
        if not pages:
            raise RuntimeError("浏览器已关闭。")
        pages[0].wait_for_timeout(min(1000, max(1, (until - time.monotonic()) * 1000)))


def run(dry_run=False):
    from playwright.sync_api import sync_playwright, Error

    state = new_state() if dry_run else load_state(STATE_PATH)
    with sync_playwright() as playwright:
        browser = None
        for channel in ("msedge", "chrome", None):
            try:
                browser = playwright.chromium.launch(headless=False, timeout=15000,
                                                       **({"channel": channel} if channel else {}))
                break
            except Error:
                continue
        if browser is None:
            raise RuntimeError("无法启动浏览器。请执行 python -m playwright install chromium 后重试。")
        try:
            context = browser.new_context(viewport=None)
            captured = []

            def capture(request):
                if urlsplit(request.url).path.endswith("/wxbgbgdz.do"):
                    try:
                        captured.append(ListRequest(request.url, request.method,
                                                    request.post_data, request.headers))
                        del captured[:-1]
                    except (ValueError, TypeError):
                        pass

            context.on("request", capture)
            page = context.new_page()
            try:
                page.goto(PORTAL, wait_until="domcontentloaded", timeout=45000)
            except Error:
                log("入口加载未完成，请在浏览器中检查网络并刷新。")
            print("\n请在新打开的浏览器中：\n"
                  "  1. 登录自己的学校账号。\n"
                  "  2. 打开“学术报告选课”的未选报告列表。\n"
                  "  3. 设置需要监控的院系筛选；开始后保持筛选范围固定。\n"
                  "启动后请保持该页面打开，电脑不要休眠。Ctrl+C 停止。\n", flush=True)
            if dry_run:
                print("当前为检查模式：不提交选课，也不改写正式状态文件。", flush=True)
            input("准备好后按回车开始每分钟检查：")
            pump_wait(context, 0.2)
            frame = find_frame(context)
            # 使用页面原生刷新方法捕获真实的查询参数。
            captured.clear()
            frame.evaluate("""() => {
                const table = $('#wxbg-index-table');
                table.emapdatatable('reload', table.emapdatatable('getCurrentQueryParams'));
            }""")
            deadline = time.monotonic() + 15
            while not captured and time.monotonic() < deadline:
                pump_wait(context, 0.2)
            if not captured:
                raise RuntimeError("未捕获到 wxbgbgdz.do 列表请求。请提供“网络”面板中列表查询的地址和字段名称，勿发送 Cookie。")
            request = captured[-1]
            log("已识别列表查询。每 60 秒直接读取后台全部分页，无需手动刷新网页。")
            for report in state["reports"].values():
                if report["status"] == "uncertain":
                    log("待手动核实：" + report.get("title", "未知报告"))
            while browser.is_connected():
                started = time.monotonic()
                try:
                    frame = find_frame(context)
                    if captured:
                        request = captured[-1]
                    log("正在向后台查询最新报告……")
                    stats = {}
                    records = read_all(lambda spec: frame.evaluate(FETCH_JS, spec), request, stats)
                except Exception as exc:
                    # 不打印原始响应、请求头或登录参数。
                    message = str(exc).split("\n", 1)[0][:220]
                    log("本轮未完成查询，不提交选课：" + message)
                else:
                    log(f"后台读取完成：{stats['pages']} 页、{stats['raw_rows']} 行，"
                        f"合并 {stats['duplicate_rows']} 条重复记录，得到 {len(records)} 个报告。")
                    if stats["conflicting_reports"]:
                        log(f"其中 {stats['conflicting_reports']} 个报告的选课状态存在冲突，暂不提交，等待下轮检查。")
                    initialized = state["initialized"]
                    fresh, candidates = observe(state, records)
                    if not dry_run:
                        save_state(STATE_PATH, state)
                    if not initialized:
                        log(f"基线已建立：{len(records)} 个现有报告不自动选课，之后只处理新增编号。")
                    else:
                        log(f"检查完成：{len(records)} 个未选报告，新增 {len(fresh)} 个，可提交 {len(candidates)} 个。")
                    for identity in fresh:
                        log("发现新增：" + state["reports"][identity]["title"])
                    for identity in candidates:
                        report = state["reports"][identity]
                        if dry_run:
                            log("[检查模式] 可选：" + report["title"])
                            continue
                        # 先写盘，再发送；中断或结果不明时不重复提交。
                        report["status"] = "submitting"
                        save_state(STATE_PATH, state)
                        log("提交选课：" + report["title"])
                        try:
                            result = frame.evaluate(SUBMIT_JS, {"bgbm": identity})
                        except Exception:
                            result = {"status": "uncertain", "message": "提交期间页面或连接异常，请手动检查已选报告。"}
                        status = result.get("status", "uncertain")
                        if status not in {"success", "rejected", "uncertain", "not_sent"}:
                            status = "uncertain"
                        report["status"] = "pending" if status == "not_sent" else status
                        report["message"] = result.get("message", "")
                        report["code"] = result.get("code", "")
                        save_state(STATE_PATH, state)
                        labels = {"success": "后台返回成功，请到已选报告核实",
                                  "rejected": "后台拒绝，不自动重复提交",
                                  "uncertain": "结果不明，停止该报告的自动提交，请手动核实",
                                  "not_sent": "尚未发出请求，下轮重试"}
                        log(labels[status] + "：" + report["title"] + " " + report["message"])
                        pump_wait(context, 1)
                pump_wait(context, max(1, INTERVAL - (time.monotonic() - started)))
        finally:
            if browser.is_connected():
                browser.close()


def main():
    parser = argparse.ArgumentParser(description="每分钟检查新增学术报告并自动选课。")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不提交，不修改正式状态文件")
    args = parser.parse_args()
    try:
        with single_instance(STATE_PATH):
            run(args.dry_run)
    except (KeyboardInterrupt, EOFError):
        log("已停止。")
    except ImportError:
        log("请先安装依赖：python -m pip install playwright")
    except Exception as exc:
        log("程序已停止：" + str(exc).split("\n", 1)[0][:250])


if __name__ == "__main__":
    main()
