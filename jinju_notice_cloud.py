#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
진주시청 고시/공고 키워드 알림 프로그램
- N분(기본 10분)마다 게시판을 확인
- 새 글 제목(옵션: 본문 포함)에 키워드가 있으면 PC 알림 + Gmail 발송
- keywords.txt 는 매 주기마다 다시 읽으므로, 프로그램을 끄지 않고도 키워드 추가 가능

사용 예)
  python jinju_notice_cloud.py                  # 계속 실행(10분 간격)
  python jinju_notice_cloud.py --once           # 1회만 확인(작업 스케줄러용)
  python jinju_notice_cloud.py --test           # PC 알림/이메일 테스트 발송
  python jinju_notice_cloud.py --add-keyword 철거
  python jinju_notice_cloud.py --remove-keyword 철거
  python jinju_notice_cloud.py --list-keywords
"""
import argparse
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
KEYWORDS_PATH = BASE_DIR / "keywords.txt"
SEEN_PATH = BASE_DIR / "seen.json"
LOG_PATH = BASE_DIR / "monitor.log"

DEFAULT_CONFIG = {
    "board_url": "https://www.jinju.go.kr/00130/02730/05586.web",
    "interval_minutes": 10,
    "check_body": False,          # True면 상세 페이지 본문까지 검사(요청 수 증가)
    "pc_notification": False,
    "email_enabled": True,
    "gmail_address": "",      # GitHub Secrets(GMAIL_ADDRESS)에서 읽음
    "gmail_app_password": "",                      # 구글 '앱 비밀번호' 16자리
    "to_email": "",           # GitHub Secrets(TO_EMAIL)에서 읽음(없으면 보내는 계정과 동일)
    "request_timeout_sec": 20,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 JinjuNoticeMonitor/1.0 (personal use)"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}

DATE_RE = re.compile(r"(20\d{2})[-./]\s?(\d{1,2})[-./]\s?(\d{1,2})")

log = logging.getLogger("jinju")


# ----------------------------------------------------------------------------
# 설정 / 키워드 / 상태 파일
# ----------------------------------------------------------------------------
def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    for env_key, cfg_key in (
        ("GMAIL_APP_PASSWORD", "gmail_app_password"),
        ("GMAIL_ADDRESS", "gmail_address"),
        ("TO_EMAIL", "to_email"),
    ):
        v = os.environ.get(env_key, "").strip()
        if v:
            cfg[cfg_key] = v
    if not cfg.get("to_email"):
        cfg["to_email"] = cfg.get("gmail_address", "")
    return cfg


def load_keywords():
    if not KEYWORDS_PATH.exists():
        defaults = ["공사", "공원", "신축", "공동주택", "아파트", "변경", "토지", "도로", "신설"]
        KEYWORDS_PATH.write_text("\n".join(defaults) + "\n", encoding="utf-8")
    words = []
    for line in KEYWORDS_PATH.read_text(encoding="utf-8").splitlines():
        w = line.strip()
        if w and not w.startswith("#") and w not in words:
            words.append(w)
    return words


def add_keyword(word):
    words = load_keywords()
    if word in words:
        print(f"이미 있는 키워드입니다: {word}")
        return
    with open(KEYWORDS_PATH, "a", encoding="utf-8") as f:
        f.write(word + "\n")
    print(f"키워드 추가 완료: {word}  (실행 중인 프로그램에는 다음 주기부터 자동 반영)")


def remove_keyword(word):
    words = load_keywords()
    if word not in words:
        print(f"없는 키워드입니다: {word}")
        return
    words.remove(word)
    KEYWORDS_PATH.write_text("\n".join(words) + "\n", encoding="utf-8")
    print(f"키워드 삭제 완료: {word}")


PARSER_VERSION = 2  # 파서를 바꾸면 올려서 기준선을 자동으로 다시 만든다


def load_seen():
    if SEEN_PATH.exists():
        try:
            data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("parser") == PARSER_VERSION:
                return data.get("urls", [])
            log.info("읽기 방식이 바뀌어 기준선을 새로 만듭니다(이번 1회는 알림 없음).")
        except Exception:
            log.warning("seen.json 을 읽을 수 없어 새로 시작합니다.")
    return None  # None = 최초 실행


def save_seen(seen):
    if len(seen) > 3000:  # 너무 커지지 않게 최근 3000건만 유지
        seen = seen[-3000:]
    SEEN_PATH.write_text(
        json.dumps({"parser": PARSER_VERSION, "urls": seen}, ensure_ascii=False), encoding="utf-8"
    )


# ----------------------------------------------------------------------------
# 게시판 수집
# ----------------------------------------------------------------------------
def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


# 글 번호처럼 '개별 글'을 가리키는 주소 파라미터 이름들
ID_KEYS = {"idx", "seq", "no", "id", "articleid", "article_id", "bbsidx", "nttid", "ntt_id",
           "boardid", "board_id", "bid", "wr_id", "num", "sn", "pkid"}


def _is_post_link(abs_url, board_url):
    """이 게시판 주소와 같은 페이지이면서, 개별 글을 가리키는 링크인가?"""
    u, b = urlparse(abs_url), urlparse(board_url)
    if u.netloc != b.netloc or u.path.rstrip("/") != b.path.rstrip("/"):
        return False  # 다른 게시판/메뉴/외부 링크 제외
    q = {k.lower(): v for k, v in parse_qs(u.query).items()}
    if any(k in ID_KEYS for k in q):
        return True
    return "view" in u.query.lower()  # amode=view, cmd=view 등


def _row_date(text):
    m = DATE_RE.search(text)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def parse_board(html_text, base_url):
    """이 게시판(base_url)의 글 목록만 (제목, 링크, 날짜)로 뽑는다."""
    soup = BeautifulSoup(html_text, "html.parser")
    candidates = []

    for container in soup.select("tr, li"):
        for a in container.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("javascript:", "#", "mailto:")):
                continue
            abs_url = urljoin(base_url, href)
            title = _clean(a.get_text())
            if len(title) < 4 or not _is_post_link(abs_url, base_url):
                continue
            candidates.append(
                {"title": title, "url": abs_url, "date": _row_date(container.get_text(" "))}
            )
            break  # 한 행에서는 첫 번째 글 링크만

    # 같은 글이 중복(상위 li + 하위 li 등)으로 잡히면 하나만
    uniq, keys = [], set()
    for it in candidates:
        if it["url"] not in keys:
            keys.add(it["url"])
            uniq.append(it)

    # 날짜가 있는 행을 우선(사이드바·배너 같은 링크 차단). 하나도 없으면 날짜 없이라도 사용
    with_date = [it for it in uniq if it["date"]]
    return with_date or uniq


def fetch_board(cfg):
    r = requests.get(cfg["board_url"], headers=HEADERS, timeout=cfg["request_timeout_sec"])
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    return parse_board(r.text, cfg["board_url"])


def fetch_body(url, cfg):
    try:
        r = requests.get(url, headers=HEADERS, timeout=cfg["request_timeout_sec"])
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer"]):
            t.decompose()
        return _clean(soup.get_text(" "))
    except Exception as e:
        log.warning("본문 조회 실패(%s): %s", url, e)
        return ""


# ----------------------------------------------------------------------------
# 알림
# ----------------------------------------------------------------------------
def notify_pc(title, message, url=None):
    """윈도우 토스트 알림(winotify) 우선, 안 되면 plyer로 대체."""
    # 1) winotify: 윈도우 10/11 알림센터에 뜨는 방식(클릭하면 해당 글 열림)
    try:
        from winotify import Notification, audio

        toast = Notification(
            app_id="진주시 고시/공고 알림",
            title=title[:60],
            msg=message[:250],
            duration="long",
            launch=url or "",  # 알림을 클릭하면 열릴 주소
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return True
    except ImportError:
        pass
    except Exception as e:
        log.warning("winotify 알림 실패: %s", e)

    # 2) plyer(대체 수단)
    try:
        from plyer import notification

        notification.notify(
            title=title[:60],
            message=message[:250],
            app_name="진주시 고시/공고 알림",
            timeout=15,
        )
        return True
    except Exception as e:
        log.warning("PC 알림 실패: %s", e)
        return False


def send_email(cfg, subject, text_body, html_body):
    if not cfg.get("gmail_app_password") or not cfg.get("gmail_address"):
        log.warning("GMAIL_APP_PASSWORD / GMAIL_ADDRESS 설정이 비어 있어 이메일을 보낼 수 없습니다.")
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg["gmail_address"]
        msg["To"] = cfg["to_email"]
        msg.set_content(text_body)
        msg.add_alternative(html_body, subtype="html")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(cfg["gmail_address"], cfg["gmail_app_password"].replace(" ", ""))
            s.send_message(msg)
        return True
    except Exception as e:
        log.error("이메일 발송 실패: %s", e)
        return False


def build_email(hits):
    """hits: [{'title','url','date','keywords'}]"""
    n = len(hits)
    first = hits[0]
    subject = f"[진주시 고시/공고] {first['title'][:40]}" + (f" 외 {n-1}건" if n > 1 else "")
    text_lines, html_rows = [], []
    for h in hits:
        kw = ", ".join(h["keywords"])
        text_lines.append(f"■ {h['title']}\n  키워드: {kw}\n  게시일: {h['date'] or '-'}\n  링크: {h['url']}\n")
        html_rows.append(
            f"<li style='margin-bottom:14px'>"
            f"<a href='{html.escape(h['url'])}' style='font-size:15px;font-weight:bold'>{html.escape(h['title'])}</a><br>"
            f"<span style='color:#555'>키워드: <b>{html.escape(kw)}</b> · 게시일: {html.escape(h['date'] or '-')}</span>"
            f"</li>"
        )
    text_body = "진주시청 고시/공고에 키워드가 포함된 새 글이 올라왔습니다.\n\n" + "\n".join(text_lines)
    html_body = (
        "<div style='font-family:Malgun Gothic,sans-serif'>"
        "<p>진주시청 고시/공고에 키워드가 포함된 새 글이 올라왔습니다.</p>"
        f"<ul style='padding-left:18px'>{''.join(html_rows)}</ul>"
        f"<p style='color:#888;font-size:12px'>확인 시각: {datetime.now():%Y-%m-%d %H:%M}</p></div>"
    )
    return subject, text_body, html_body


# ----------------------------------------------------------------------------
# 메인 로직
# ----------------------------------------------------------------------------
def run_once(cfg, notify_existing=False):
    keywords = load_keywords()
    items = fetch_board(cfg)
    if not items:
        raise RuntimeError("게시글을 하나도 찾지 못했습니다. 사이트 구조 변경 또는 접속 차단 가능성")

    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    seen_set = set(seen)

    new_items = [it for it in items if it["url"] not in seen_set]

    if first_run and not notify_existing:
        log.info("최초 실행: 현재 게시글 %d건을 기준선으로 저장합니다(알림 없음).", len(items))
        save_seen([it["url"] for it in reversed(items)])
        return

    log.info("확인 완료: 전체 %d건 중 새 글 %d건 (키워드 %d개)", len(items), len(new_items), len(keywords))

    hits, handled = [], []
    for it in reversed(new_items):  # 오래된 글부터 처리
        matched = [k for k in keywords if k in it["title"]]
        if cfg.get("check_body") and not matched:
            body = fetch_body(it["url"], cfg)
            matched = [k for k in keywords if k in body]
        if matched:
            hits.append({**it, "keywords": matched})
        else:
            handled.append(it["url"])  # 키워드 없는 글은 바로 확인 처리

    if hits:
        pc_ok = mail_ok = False
        if cfg.get("pc_notification"):
            for h in hits:
                pc_ok |= notify_pc(f"[{', '.join(h['keywords'])}] 진주시 고시/공고", h["title"], h["url"])
        if cfg.get("email_enabled"):
            subject, text_body, html_body = build_email(hits)
            mail_ok = send_email(cfg, subject, text_body, html_body)
        if pc_ok or mail_ok:
            handled += [h["url"] for h in hits]
            log.info("알림 발송: %d건 (PC=%s, 이메일=%s)", len(hits), pc_ok, mail_ok)
        else:
            log.error("알림을 하나도 보내지 못해 다음 주기에 다시 시도합니다.")

    save_seen(seen + handled)


def run_replay(cfg, n):
    """최근 n건을 '새 글'인 것처럼 검사해서 키워드에 걸리면 실제로 알림을 보낸다(기록은 바꾸지 않음)."""
    keywords = load_keywords()
    items = fetch_board(cfg)[:n]
    hits = []
    for it in items:
        matched = [k for k in keywords if k in it["title"]]
        if matched:
            hits.append({**it, "keywords": matched})
    print(f"최근 {len(items)}건 중 키워드에 해당하는 글: {len(hits)}건")
    for h in hits:
        print(f"  - {h['date']} | {h['title'][:45]} | 키워드: {', '.join(h['keywords'])}")
    if not hits:
        print("해당하는 글이 없어 알림을 보내지 않습니다. 건수(N)를 늘려 보세요.")
        return
    pc_ok = mail_ok = None
    if cfg.get("pc_notification"):
        pc_ok = False
        for h in hits:
            pc_ok |= notify_pc(f"[{', '.join(h['keywords'])}] 진주시 고시/공고", h["title"], h["url"])
    if cfg.get("email_enabled"):
        subject, text_body, html_body = build_email(hits)
        mail_ok = send_email(cfg, subject, text_body, html_body)
    fmt = lambda v: "꺼짐" if v is None else ("성공" if v else "실패")
    print(f"PC 알림: {fmt(pc_ok)} / 이메일: {fmt(mail_ok)}")


def run_test(cfg):
    print("테스트 알림을 보냅니다...")
    pc = notify_pc("[테스트] 진주시 고시/공고 알림", "PC 알림이 정상 동작합니다.", cfg["board_url"]) if cfg.get("pc_notification") else None
    hits = [{
        "title": "[테스트] 아파트 신축 공사 관련 변경 고시",
        "url": cfg["board_url"],
        "date": f"{datetime.now():%Y-%m-%d}",
        "keywords": ["아파트", "신축", "공사", "변경"],
    }]
    subject, t, h = build_email(hits)
    mail = send_email(cfg, subject, t, h) if cfg.get("email_enabled") else None
    print(f"PC 알림: {'꺼짐' if pc is None else ('성공' if pc else '실패')} / 이메일: {'성공' if mail else ('꺼짐' if mail is None else '실패')}")
    if mail is False:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="진주시청 고시/공고 키워드 알림")
    ap.add_argument("--once", action="store_true", help="1회만 확인하고 종료")
    ap.add_argument("--test", action="store_true", help="PC 알림/이메일 테스트")
    ap.add_argument("--notify-existing", action="store_true", help="최초 실행 때 현재 글에도 알림")
    ap.add_argument("--add-keyword", metavar="WORD")
    ap.add_argument("--remove-keyword", metavar="WORD")
    ap.add_argument("--list-keywords", action="store_true")
    ap.add_argument("--replay", type=int, metavar="N", help="최근 N건을 새 글처럼 검사해 알림 테스트(기록은 변경 안 함)")
    ap.add_argument("--dump-html", action="store_true", help="게시판 페이지 원본을 board_page.html 로 저장(점검용)")
    ap.add_argument("--show-parsed", action="store_true", help="게시판에서 읽어낸 글 목록을 화면에 출력(점검용)")
    args = ap.parse_args()

    if args.add_keyword:
        return add_keyword(args.add_keyword.strip())
    if args.remove_keyword:
        return remove_keyword(args.remove_keyword.strip())
    if args.list_keywords:
        print("\n".join(load_keywords()))
        return

    setup_logging()
    cfg = load_config()

    if args.test:
        return run_test(cfg)

    if args.replay:
        return run_replay(cfg, args.replay)

    if args.dump_html:
        r = requests.get(cfg["board_url"], headers=HEADERS, timeout=cfg["request_timeout_sec"])
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        (BASE_DIR / "board_page.html").write_text(r.text, encoding="utf-8")
        print(f"저장 완료: {BASE_DIR / 'board_page.html'}  ({len(r.text):,}자)")
        return

    if args.show_parsed:
        items = fetch_board(cfg)
        print(f"읽어낸 글: {len(items)}건")
        for i, it in enumerate(items, 1):
            if i <= 20 or i > len(items) - 5:
                print(f"{i:>4} | {it['date'] or '날짜없음':10} | {it['title'][:50]} | {it['url']}")
            elif i == 21:
                print("  ... (중간 생략) ...")
        return

    if args.once:
        try:
            run_once(cfg, args.notify_existing)
        except Exception:
            log.exception("확인 중 오류")
            sys.exit(1)
        return

    log.info("감시 시작: %s (%d분 간격)", cfg["board_url"], cfg["interval_minutes"])
    first = True
    while True:
        try:
            cfg = load_config()  # 설정도 매 주기 다시 읽음
            run_once(cfg, args.notify_existing and first)
            first = False
        except requests.RequestException as e:
            log.warning("네트워크 오류(다음 주기에 재시도): %s", e)
        except Exception:
            log.exception("예상치 못한 오류(다음 주기에 재시도)")
        time.sleep(max(1, int(cfg["interval_minutes"])) * 60)


if __name__ == "__main__":
    main()
