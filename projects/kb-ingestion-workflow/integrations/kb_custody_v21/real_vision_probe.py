#!/usr/bin/env python3
"""Explicit synthetic-only official API probe. Never invoked by offline CI."""
import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import ssl
import sys

from PIL import Image, ImageDraw

HOST = "api.deepseek.com"
MODEL = "deepseek-flash"
RESERVATION_CNY = Decimal("2.50")
CAP_CNY = Decimal("3")
MAX_OUTPUT = 512
# Conservative peak, cache-miss prices, verified on the official CNY page.
INPUT_PER_MILLION, OUTPUT_PER_MILLION = Decimal("2"), Decimal("8")
PRICE_DATE = "2026-10-01"
PROMPT = '请只返回 JSON：{"blue_squares":整数,"red_circles":整数,"label":"图片中的文字"}。按图片实际内容计数，不要猜测。'


def fixture():
    image = Image.new("RGB", (384, 256), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 60, 100, 130), fill="blue")
    draw.rectangle((135, 60, 205, 130), fill="blue")
    draw.ellipse((250, 60, 320, 130), fill="red")
    draw.text((115, 180), "KB21", fill="black", font_size=30)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def reserve(path, payload_sha, available):
    if available < RESERVATION_CNY or RESERVATION_CNY > CAP_CNY:
        raise ValueError("insufficient balance/budget")
    # Exclusive creation is the irreversible request intent. A crash/timeout
    # consumes the reservation and forbids another paid request in this run.
    record = {"state": "reserved_unknown", "maximum_requests": 1, "automatic_retries": 0,
              "reserved_cny": str(RESERVATION_CNY), "cap_cny": str(CAP_CNY),
              "payload_sha256": payload_sha, "price_date": PRICE_DATE}
    with path.open("x") as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


def cost_upper(usage):
    prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
    if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
        raise ValueError("invalid provider usage")
    if prompt > 1_048_576 or completion > MAX_OUTPUT:
        raise ValueError("provider exceeded bounded token contract")
    return (prompt * INPUT_PER_MILLION + completion * OUTPUT_PER_MILLION) / 1_000_000


def request(key, method, path, payload=None):
    # Fixed official HTTPS host, verified TLS, no redirect/proxy/automatic retry.
    connection = http.client.HTTPSConnection(HOST, timeout=45, context=ssl.create_default_context())
    try:
        connection.request(method, path, body=payload,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or response.status != 200:
            raise RuntimeError("official_api_http_status_" + str(response.status))
        return json.loads(raw)
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).absolute()
    base = Path.cwd() / ".dev-runtime"
    if not output.is_relative_to(base) or any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("private evidence must stay inside this worktree/.dev-runtime")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    png = fixture()
    (output / "synthetic-vision.png").write_bytes(png)
    body = json.dumps({"model": MODEL, "max_tokens": MAX_OUTPUT,
        "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]}).encode()
    plan = {"model": MODEL, "host": HOST, "image_sha256": hashlib.sha256(png).hexdigest(),
            "image_bytes": len(png), "payload_sha256": hashlib.sha256(body).hexdigest(),
            "max_model_requests": 1, "max_balance_queries": 2, "max_output_tokens": MAX_OUTPUT,
            "automatic_retries": 0, "reservation_cny": str(RESERVATION_CNY), "total_cap_cny": str(CAP_CNY),
            "price_date": PRICE_DATE, "input_peak_cny_per_million": "2", "output_peak_cny_per_million": "8"}
    (output / "plan.local.json").write_text(json.dumps(plan, indent=2))
    if not args.execute:
        print(json.dumps(plan))
        return
    if datetime.now(timezone.utc).date().isoformat() != PRICE_DATE:
        raise ValueError("pricing must be reviewed again before executing on another date")
    ledger = output / "paid-request-intent.local.json"
    if ledger.exists():
        raise ValueError("one-request intent already exists; no resend permitted")
    key = sys.stdin.read().strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError("credential required on private stdin")
    balance = request(key, "GET", "/user/balance")
    balances = [Decimal(b["total_balance"]) for b in balance["balance_infos"] if b["currency"] == "CNY"]
    if not balance["is_available"] or len(balances) != 1:
        raise ValueError("usable CNY balance required")
    record = reserve(ledger, plan["payload_sha256"], balances[0])
    try:
        response = request(key, "POST", "/chat/completions", body)
        (output / "response.local.json").write_text(json.dumps(response, ensure_ascii=False, indent=2))
        upper = cost_upper(response["usage"])
        parsed = json.loads(response["choices"][0]["message"]["content"])
        success = parsed == {"blue_squares": 2, "red_circles": 1, "label": "KB21"}
        record.update(state="response_received", response_model=response.get("model"), usage=response["usage"],
                      usage_cost_upper_cny=str(upper), expected_answer_passed=success, answer=parsed,
                      reservation_retained_cny=str(RESERVATION_CNY),
                      limitation="Synthetic API contract only; no real document or end-to-end acceptance.")
        # Balance differences are private and may include other concurrent work.
        after = request(key, "GET", "/user/balance")
        (output / "balances.local.json").write_text(json.dumps({"before": balance, "after": after}))
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, http.client.HTTPException) as error:
        record.update(error_type=type(error).__name__, stopped=True)
        raise RuntimeError("probe stopped; reservation retained; inspect private evidence") from None
    finally:
        (output / "result.local.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
