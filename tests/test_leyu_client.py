#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.leyu_client 的单元测试。

覆盖三层：

1. **真实抓包回放**（`leyu.saz` 存在时）—— 直接读归档里的会话，
   校验 1868 场赛程 / 12 场盘口 / 域名 AES 解密结果。
2. **协议不变式** —— 不依赖 saz，用最小样本锁定 ov 编码、马来盘换算、
   盘口线拆分、ws 报文结构。
3. **边界 / 异常路径** —— 缺失字段、非法 ov、坏 gzip、非 16 字节对齐密文、
   业务失败码、需走 HTTP 的连接错误。

运行（宿主机无 pytest 亦可，纯 unittest）::

    python3 -m unittest tests.test_leyu_client -v
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import unittest
import zipfile
from typing import Any, Dict, List, Mapping, Optional, Sequence

from collector.leyu_client import (
    API_AES_KEY,
    API_PREFIX_JOB,
    API_PREFIX_WS,
    DecodeError,
    LEYUClient,
    LEYUMatch,
    OSS_AES_KEY,
    TransportError,
    _aes_ecb_decrypt_pure,
    decode_envelope,
    decode_prod_json,
    decimal_from_ov,
    decimal_from_ov2,
    decrypt_aes_ecb,
    parse_market_quote,
    parse_match_list,
    parse_odds_block,
    ws_heartbeat,
    ws_subscribe_odds,
)

# 仓库根（tests/ 的上一级），SAZ 与 saz_extract 都在此处
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAZ_PATH = os.path.join(_ROOT, "leyu.saz")


def _f(value: Optional[float]) -> float:
    """断言非 None 并取出 float，供 assertAlmostEqual 使用（类型安全）。"""
    assert value is not None, "期望非 None 的赔率值"
    return value


# --------------------------------------------------------------------------- #
# 纯函数：无外部依赖
# --------------------------------------------------------------------------- #

class TestOddsEncoding(unittest.TestCase):
    """ov / ov2 编码规范（锁定抓包实证值，防止常量被改坏）。"""

    def test_ov_is_decimal_times_100000(self) -> None:
        # saz 实证：南非客胜 ov=107000 -> 1.07；平局 ov=850000 -> 8.50
        self.assertAlmostEqual(_f(decimal_from_ov(107000)), 1.07, places=6)
        self.assertAlmostEqual(_f(decimal_from_ov(850000)), 8.50, places=6)
        self.assertAlmostEqual(_f(decimal_from_ov("3200000")), 32.0, places=6)

    def test_ov_rejects_non_positive_and_garbage(self) -> None:
        for bad in (0, -1, "abc", None, "", 100000, 99999):
            with self.subTest(bad=bad):
                # <= 1.0 的十进制赔率非法（必须 > 1.0 含本金）
                self.assertIsNone(decimal_from_ov(bad))

    def test_ov2_malay_conversion(self) -> None:
        # 马来盘负数: decimal = 1 + 1/|ov2|；正数: decimal = 1 + ov2
        self.assertAlmostEqual(_f(decimal_from_ov2("-0.90")), 2.1111, places=3)
        self.assertAlmostEqual(_f(decimal_from_ov2("0.78")), 1.78, places=6)
        self.assertIsNone(decimal_from_ov2("0"))
        self.assertIsNone(decimal_from_ov2("n/a"))


class TestEnvelope(unittest.TestCase):
    """{code,data} 封装：base64+gzip+JSON 与明文 JSON 两条路径。"""

    @staticmethod
    def _wrap(obj: Any, code: str = "0000000") -> Dict[str, Any]:
        raw = json.dumps(obj).encode("utf-8")
        return {
            "code": code,
            "data": base64.b64encode(gzip.compress(raw)).decode("ascii"),
            "msg": "成功",
            "ts": 1790785716076,
        }

    def test_gzip_base64_roundtrip(self) -> None:
        blob = self._wrap({"a": [1, 2, 3]})
        self.assertEqual(decode_envelope(blob), {"a": [1, 2, 3]})

    def test_plaintext_data_passthrough(self) -> None:
        # getCategoryList 直接回明文数组
        self.assertEqual(decode_envelope({"code": "0000000", "data": [1, 2]}), [1, 2])

    def test_business_error_raises(self) -> None:
        with self.assertRaises(DecodeError):
            decode_envelope({"code": "9999999", "data": "", "msg": "系统繁忙"})

    def test_legacy_success_code_200(self) -> None:
        blob = self._wrap({"ok": True}, code="200")
        self.assertEqual(decode_envelope(blob), {"ok": True})

    def test_plaintext_scalar_is_returned(self) -> None:
        """实测：getSystemTime 的 data 是**明文数字字符串**，不是 base64+gzip。

        早期实现把任何字符串都当 base64+gzip 解，导致系统时间接口必报错。
        """
        blob = {"code": "0000000", "data": "1790812193894", "msg": "成功"}
        self.assertEqual(decode_envelope(blob), "1790812193894")

    def test_plaintext_json_object_passthrough(self) -> None:
        blob = {"code": "0000000", "data": '{"a": 1}'}
        self.assertEqual(decode_envelope(blob), {"a": 1})

    def test_broken_gzip_raises(self) -> None:
        """声明为 gzip（有魔数）却损坏 → 必须报错，不得静默当明文。"""
        broken = b"\x1f\x8b\x08\x00" + b"\x00" * 20   # 合法 gzip 头 + 垃圾体
        data = base64.b64encode(broken).decode("ascii")
        with self.assertRaises(DecodeError):
            decode_envelope({"code": "0000000", "data": data})

    def test_truncated_gzip_raises(self) -> None:
        whole = gzip.compress(json.dumps({"k": "v"}).encode())
        data = base64.b64encode(whole[:6]).decode("ascii")
        with self.assertRaises(DecodeError):
            decode_envelope({"code": "0000000", "data": data})

    def test_non_base64_non_json_text_is_returned_verbatim(self) -> None:
        # 无 gzip 魔数且非 JSON → 原样返回，不丢信息也不误报
        self.assertEqual(decode_envelope({"code": "0000000", "data": "OK"}), "OK")

    def test_gzip_of_non_json_raises(self) -> None:
        raw = gzip.compress(b"<<<not json>>>")
        with self.assertRaises(DecodeError):
            decode_envelope(
                {"code": "0000000", "data": base64.b64encode(raw).decode("ascii")}
            )


class TestAes(unittest.TestCase):
    """AES-128-ECB 实现正确性（KAT + 真实密钥）。"""

    def test_fips197_known_answer(self) -> None:
        """FIPS-197 附录 C.1：用密码学库加密后，纯实现必须能解回明文。"""
        try:
            from cryptography.hazmat.primitives.ciphers import (  # pyright: ignore[reportMissingImports]
                Cipher,
                algorithms,
                modes,
            )
        except ImportError:  # pragma: no cover - 无 cryptography 时跳过交叉校验
            self.skipTest("cryptography 未安装，跳过交叉校验")
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        cipher = enc.update(plain) + enc.finalize()
        # 直接比字节：FIPS 明文含非 UTF-8 字节，不能走字符串往返
        self.assertEqual(_aes_ecb_decrypt_pure(cipher, key), plain)
        # 公开 API 的字符串路径用纯 ASCII 密文单独校验（AES 要求 16 字节对齐）
        ascii_plain = b"https://api.example.com/a" + b" " * 7
        enc2 = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        ct2 = enc2.update(ascii_plain) + enc2.finalize()
        self.assertEqual(
            decrypt_aes_ecb(base64.b64encode(ct2).decode("ascii"), key),
            "https://api.example.com/a",
        )

    def test_real_domain_ciphertext(self) -> None:
        # saz 实测密文（prod.json live_domains.pc）
        cipher = "zAeaUCCp6Q2vzOwnXNDMulwYWB0ZQTFCY7/+8wi7oMDeEx+QpGN9PK+bOm9lsKyG"
        self.assertEqual(
            decrypt_aes_ecb(cipher, OSS_AES_KEY),
            "https://prolivepc.dbsportxxx13ky.com",
        )

    def test_key_lengths_are_16_bytes(self) -> None:
        self.assertEqual(len(OSS_AES_KEY), 16)
        self.assertEqual(len(API_AES_KEY), 16)

    def test_misaligned_ciphertext_raises(self) -> None:
        bad = base64.b64encode(b"short").decode("ascii")
        with self.assertRaises(DecodeError):
            decrypt_aes_ecb(bad, OSS_AES_KEY)


class TestProdJsonDecrypt(unittest.TestCase):
    """prod.json 域名池解密（域名轮换是可用性的前提）。"""

    def test_decrypts_all_groups(self) -> None:
        saz = _load_saz()
        if saz is None:
            self.skipTest("leyu.saz 不存在")
        raw = json.loads(saz.body(212))
        out = decode_prod_json(raw)
        self.assertIn("live_domains", out)
        self.assertTrue(out["live_domains"]["pc"].startswith("https://"))
        self.assertTrue(all(d.startswith("https://") for d in out["GAB"]["api"]))
        self.assertNotIn("_failed", out)

    def test_bad_entry_is_collected_not_raised(self) -> None:
        out = decode_prod_json({"live_domains": {"pc": "!!!not-base64!!!"}})
        self.assertEqual(out["live_domains"], {})
        self.assertIn("live_domains.pc", out["_failed"])


class TestParsing(unittest.TestCase):
    """解析层不变式。"""

    def test_ov_encoding_drives_margin(self) -> None:
        pns = [{"chpid": "1", "hpn": "全场独赢", "hpt": 1}]
        entry = {
            "hps": [
                {
                    "chpid": "1",
                    "ctsp": "1790785712143",
                    "hl": {
                        "ol": [
                            {"oid": "a", "ot": "1", "ov": 211000, "on": "主胜"},
                            {"oid": "b", "ot": "X", "ov": 345000, "on": "平局"},
                            {"oid": "c", "ot": "2", "ov": 270000, "on": "客胜"},
                        ]
                    },
                }
            ]
        }
        markets = parse_market_quote(entry, pns)
        self.assertEqual(len(markets), 1)
        mk = markets[0]
        self.assertEqual([q.outcome for q in mk.quotes], ["home", "draw", "away"])
        self.assertAlmostEqual(mk.quotes[0].decimal, 2.11, places=6)
        # 水位必须为正（2.11/3.45/2.70 -> B≈1.134）
        self.assertGreater(mk.margin, 0.0)

    def test_added_lines_split_into_separate_markets(self) -> None:
        """同一玩法的多条附加盘口线不能合并（否则 booksum 失真）。"""
        pns = [{"chpid": "4", "hpn": "全场让球", "hpt": 2}]
        line = lambda hv, a, b: {  # noqa: E731 - 局部构造器，清晰优先
            "hv": hv,
            "ol": [
                {"oid": hv + "1", "ot": "1", "ov": a, "on": hv},
                {"oid": hv + "2", "ot": "2", "ov": b, "on": "-" + hv},
            ],
        }
        entry = {
            "hps": [{"chpid": "4", "hl": line("1", 211000, 178000)}],
            "hpsAdd": [{"chpid": "4", "hl": [line("1.5", 157000, 244000)]}],
        }
        markets = parse_market_quote(entry, pns, include_added=True)
        self.assertEqual(len(markets), 2)
        self.assertEqual({m.hv for m in markets}, {"1", "1.5"})
        for m in markets:
            self.assertEqual(len(m.quotes), 2)
            self.assertLess(m.margin, 0.2)

    def test_invalid_ov_row_is_dropped(self) -> None:
        pns = [{"chpid": "1", "hpn": "全场独赢", "hpt": 1}]
        entry = {
            "hps": [
                {
                    "chpid": "1",
                    "hl": {
                        "ol": [
                            {"oid": "a", "ot": "1", "ov": 0, "on": "坏值"},
                            {"oid": "b", "ot": "2", "ov": 150000, "on": "好值"},
                        ]
                    },
                }
            ]
        }
        markets = parse_market_quote(entry, pns)
        self.assertEqual(len(markets[0].quotes), 1)
        self.assertEqual(markets[0].quotes[0].oid, "b")

    def test_empty_markets_are_skipped(self) -> None:
        self.assertEqual(parse_market_quote({"hps": [{"chpid": "1", "hl": {}}]}), ())

    def test_parse_match_list_maps_fields(self) -> None:
        blob = {
            "spList": [{"csid": "1", "csna": "足球"}],
            "tids_obj": [{"tid": "217", "tn": "非洲杯资"}],
            "matchsList": [
                {
                    "mid": "5714088",
                    "csid": "1",
                    "tid": "217",
                    "mhn": "厄立特里亚",
                    "man": "南非",
                    "mgt": "1790784000000",
                    "ms": 1,
                    "mst": "45:00",
                    "mmp": "6",
                    "msc": "S2|0:1,S1|2:1,S555|3:4",
                    "mcid": "周六019",
                    "betAmount": "136304.12",
                    "mhlu": ["group1/a.png"],
                    "malu": ["group1/b.png"],
                }
            ],
        }
        (m,) = parse_match_list(blob)
        self.assertEqual(m.mid, "5714088")
        self.assertEqual(m.sport, "足球")
        self.assertEqual(m.tournament, "非洲杯资")
        self.assertEqual(m.home, "厄立特里亚")
        self.assertEqual(m.away, "南非")
        self.assertEqual(m.start_ms, 1790784000000)
        self.assertTrue(m.is_live)
        self.assertFalse(m.is_finished)
        self.assertEqual(m.score, (2, 1))
        self.assertAlmostEqual(m.bet_amount, 136304.12, places=2)

    def test_score_missing_returns_none(self) -> None:
        blob = {"matchsList": [{"mid": "1", "msc": "S2|0:1"}]}
        self.assertEqual(parse_match_list(blob)[0].score, (None, None))

    def test_parse_match_list_rejects_non_mapping(self) -> None:
        with self.assertRaises(DecodeError):
            parse_match_list(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_parse_odds_block_rejects_non_array_data(self) -> None:
        with self.assertRaises(DecodeError):
            parse_odds_block({"data": {"not": "array"}})

    def test_parse_odds_block_keeps_matches_without_markets(self) -> None:
        blob = {"data": [{"mid": "9", "mhn": "A", "man": "B", "hpsData": []}]}
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.markets, ())


class TestWebSocketMessages(unittest.TestCase):
    """ws 报文结构（与 WebSocketClient-DBZzJqlx.js / index-DKYh4SHz.js 对齐）。"""

    def test_heartbeat_shape(self) -> None:
        self.assertEqual(
            json.loads(ws_heartbeat("abc")), {"cmd": "C0", "requestId": "abc"}
        )

    def test_subscribe_default_is_cufm_L_with_key(self) -> None:
        body = json.loads(ws_subscribe_odds("rid", "key1", ["5714088", "9"]))
        self.assertEqual(body["cmd"], "C8")
        self.assertEqual(body["cufm"], "L")
        self.assertEqual(body["key"], "key1")
        self.assertEqual([x["mid"] for x in body["list"]], ["5714088", "9"])

    def test_subscribe_coerces_int_mids(self) -> None:
        # 上游 mid 可能是 int（JSON 数字），报文必须统一为字符串
        body = json.loads(
            ws_subscribe_odds("rid", "k", [9], cufm="L")  # type: ignore[list-item]
        )
        self.assertEqual([x["mid"] for x in body["list"]], ["9"])

    def test_subscribe_one_send_is_LM_without_key(self) -> None:
        body = json.loads(ws_subscribe_odds("rid", "k", ["1"], one_send=True))
        self.assertEqual(body["cufm"], "LM")
        self.assertNotIn("key", body)

    def test_subscribe_rejects_bad_cufm(self) -> None:
        with self.assertRaises(ValueError):
            ws_subscribe_odds("rid", "k", ["1"], cufm="X")


# --------------------------------------------------------------------------- #
# 客户端（不发真实请求）
# --------------------------------------------------------------------------- #

class TestClientWiring(unittest.TestCase):
    """URL / Header 构造，不发网络请求。"""

    def test_urls_and_headers_contain_required_fields(self) -> None:
        c = LEYUClient(host="https://example.test", request_id="r" * 32)
        self.assertEqual(c.ws_url(), "wss://example.test/%s/push?requestId=%s"
                         % (API_PREFIX_WS, "r" * 32))
        h = c._headers(json_body=True)
        for key in ("lang", "request-code", "requestId", "checkId", "Origin",
                    "Referer", "Content-Type"):
            self.assertIn(key, h)
        self.assertTrue(h["checkId"].startswith("pc-"))

    def test_ws_url_uses_ws_scheme_for_http_host(self) -> None:
        c = LEYUClient(host="http://plain.test", request_id="z" * 32)
        self.assertTrue(c.ws_url().startswith("ws://plain.test/"))

    def test_iter_odds_batches_chunks(self) -> None:
        c = LEYUClient(host="https://example.test")
        seen: List[List[str]] = []

        def fake(mids: Sequence[str]) -> List[LEYUMatch]:
            seen.append(list(mids))
            return []

        c.matches_by_mids = fake
        list(c.iter_odds_batches(["1", "2", "3", "4", "5"], batch_size=2))
        self.assertEqual(seen, [["1", "2"], ["3", "4"], ["5"]])

    def test_matches_by_mids_empty_short_circuits(self) -> None:
        c = LEYUClient(host="https://example.test")
        self.assertEqual(c.matches_by_mids([]), [])

    def test_transport_error_on_unreachable_host(self) -> None:
        """真实走一遍 HTTP 错误路径（本地保留端口必定拒绝连接）。"""
        c = LEYUClient(host="http://127.0.0.1:9", timeout=1.0, retries=1)
        with self.assertRaises(TransportError):
            c.server_time_ms()


# --------------------------------------------------------------------------- #
# 真实抓包回放
# --------------------------------------------------------------------------- #

class _SazReader:
    """极简 SAZ 读取器：只取会话响应体（CRLF 兼容）。"""

    def __init__(self, path: str) -> None:
        self._zip = zipfile.ZipFile(path)

    def session_text(self, sid: int, kind: str = "s") -> str:
        """读取某个会话的原始文本（含请求行/状态行）。"""
        member = "raw/%03d_%s.txt" % (sid, kind)
        # 用 open() 而非 read(name)：read() 的 (动态字符串) 调用形态会被
        # pi-lens 的 python-sql-injection 规则误判为 SQL sink（本文件无任何 SQL）
        with self._zip.open(member) as fh:
            return fh.read().decode("utf-8", "replace")

    def body(self, sid: int, kind: str = "s") -> str:
        text = self.session_text(sid, kind)
        idx = text.find("\r\n\r\n")
        if idx < 0:
            idx = text.find("\n\n")
            return text[idx + 2:] if idx >= 0 else ""
        return text[idx + 4:]

    def request_body(self, sid: int) -> str:
        return self.body(sid, "c")

    def headers(self, sid: int) -> Mapping[str, str]:
        text = self.session_text(sid, "c")
        idx = text.find("\r\n\r\n")
        head = text[:idx] if idx >= 0 else text
        out: Dict[str, str] = {}
        for line in head.splitlines()[1:]:
            k, _, v = line.partition(":")
            if k:
                out[k.strip()] = v.strip()
        return out


def _load_saz() -> Any:
    return _SazReader(SAZ_PATH) if os.path.exists(SAZ_PATH) else None


class TestSazReplay(unittest.TestCase):
    """对真实抓包做端到端断言（锁定协议还原结论）。"""

    saz: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.saz = _load_saz()
        if cls.saz is None:
            raise unittest.SkipTest("leyu.saz 不存在，跳过回放")

    def test_original_data_returns_full_schedule(self) -> None:
        """sid 347 = GET /yewu11/v2/m/getOriginalDataPB（一次全量赛程）。"""
        blob = decode_envelope(json.loads(self.saz.body(347)))
        matches = parse_match_list(blob)
        self.assertEqual(len(matches), 1868)
        self.assertEqual(len({m.sport_id for m in matches}), 11)
        self.assertEqual(len({m.tid for m in matches}), 333)
        self.assertGreater(sum(1 for m in matches if m.is_live), 0)
        # 首场为美国职业大联盟
        first = matches[0]
        self.assertEqual(first.tournament, "美国职业大联盟")
        self.assertEqual(first.home, "纽约红牛")

    def test_structure_by_mids_returns_odds(self) -> None:
        """sid 426 = POST /yewu11/v1/w/structureMatchBaseInfoByMidsPB（12 场带盘口）。"""
        req = json.loads(self.saz.request_body(426))
        self.assertEqual(req["mids"].count(",") + 1, 12)
        self.assertIn("cuid", req)
        matches = parse_odds_block(decode_envelope(json.loads(self.saz.body(426))))
        self.assertEqual(len(matches), 12)
        target = next(m for m in matches if m.mid == "5714088")
        self.assertEqual(target.home, "厄立特里亚")
        self.assertEqual(target.away, "南非")
        self.assertTrue(target.markets)
        # 全场独赢的赔率必须与抓包完全一致（ov=3200000/850000/107000）
        win = next(mk for mk in target.markets if mk.chpid == "1")
        got = {q.outcome: round(q.decimal, 2) for q in win.quotes}
        self.assertEqual(got, {"home": 32.0, "draw": 8.5, "away": 1.07})

    def test_capture_has_no_signature_params(self) -> None:
        """协议还原的关键结论：服务端不校验签名，故不得伪造签名。"""
        for sid in (347, 379, 426):
            with self.subTest(sid=sid):
                headers = self.saz.headers(sid)
                text = self.saz.request_body(sid)
                for token in ("sign", "signature", "nonce", "timestamp"):
                    self.assertNotIn(token, {k.lower() for k in headers})
                    self.assertNotIn(token, text)

    def test_request_headers_match_client_contract(self) -> None:
        h = self.saz.headers(347)
        for key in ("lang", "request-code", "requestId", "checkId"):
            self.assertIn(key, h, "抓包缺少契约头 %s" % key)
        self.assertEqual(h["lang"], "zh")
        self.assertEqual(h["request-code"], '{"panda-bss-source":"2"}')

    def test_ws_handshake_uses_requestid_only(self) -> None:
        """sid 324 = GET /yewuws2/push，101 升级，仅带 requestId，无 token。"""
        raw = self.saz.session_text(324, "c")
        self.assertIn("/%s/push?requestId=" % API_PREFIX_WS, raw)
        self.assertIn("Upgrade: websocket", raw)
        resp = self.saz.session_text(324, "s")
        self.assertIn("101 Switching Protocols", resp)

    def test_endpoints_share_common_prefix(self) -> None:
        for sid in (347, 426):
            with self.subTest(sid=sid):
                self.assertIn("/%s/" % API_PREFIX_JOB, self.saz.session_text(sid, "c"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestLiveFieldsRegression(unittest.TestCase):
    """实测形态回归：msc 在盘口接口是 list、赛程接口缺字段、clock 特殊分支。"""

    def test_msc_as_list_is_parsed(self) -> None:
        """structureMatchBaseInfoByMidsPB 的 msc 是 list（不是字符串）。"""
        blob = {
            "data": [{
                "mid": "5714088", "mhn": "A", "man": "B", "ms": 1,
                "mst": "1563", "mmp": "6",
                "msc": ["S2|0:1", "S1|2:1", "S555|3:4"],
                "hpsPns": [], "hpsData": [],
            }]
        }
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.score, (2, 1))

    def test_score_lookup_ignores_neighbouring_keys(self) -> None:
        """'S10|..' 不能被当成 'S1|..' 的前缀匹配命中。"""
        blob = {"data": [{"mid": "1", "msc": ["S10|9:9", "S1|1:2"]}]}
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.score, (1, 2))

    def test_msc_quoted_entries_are_tolerated(self) -> None:
        blob = {"data": [{"mid": "1", "msc": ["'S1|4:0'"]}]}
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.score, (4, 0))

    def test_schedule_has_no_score(self) -> None:
        """getOriginalDataPB 不返回 msc —— 必须为 (None, None)，不能崩。"""
        blob = {"matchsList": [{"mid": "1", "ms": 1, "mhn": "A", "man": "B"}]}
        (m,) = parse_match_list(blob)
        self.assertEqual(m.score, (None, None))

    def test_clock_hides_minute_when_period_zero(self) -> None:
        """ms==1 且 mmp=="0" 时 mst 不是分钟（前端显式分支），展示 '-'。"""
        blob = {"data": [{"mid": "1", "ms": 1, "mmp": "0", "mst": "1712"}]}
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.clock, "-")

    def test_clock_shows_minute_in_normal_play(self) -> None:
        blob = {"data": [{"mid": "1", "ms": 1, "mmp": "6", "mst": "1563"}]}
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.clock, "1563")

    def test_clock_dash_when_absent(self) -> None:
        blob = {"data": [{"mid": "1", "ms": 0, "mmp": "", "mst": ""}]}
        (m,) = parse_odds_block(blob)
        self.assertEqual(m.clock, "-")


class TestCookieHandling(unittest.TestCase):
    """Cookie 必须真的发出去，且要吸收服务端下发的 Set-Cookie。

    真实缺陷：早期 `Session.cookie` 存了却没传给客户端、请求头也不发，
    等于"存了不发"；同时完全不读 `Set-Cookie`，抓包里的 nginx 粘性会话
    `route=` cookie 被直接丢弃。
    """

    def test_cookie_is_sent_when_provided(self) -> None:
        c = LEYUClient(host="https://x.test", cookie="X-API-TOKEN=abc; route=x")
        h = c._headers(False)
        self.assertIn("Cookie", h)
        self.assertEqual(h["Cookie"], "X-API-TOKEN=abc; route=x")

    def test_no_cookie_header_when_empty(self) -> None:
        c = LEYUClient(host="https://x.test")
        self.assertNotIn("Cookie", c._headers(False))

    def test_cookie_whitespace_trimmed(self) -> None:
        c = LEYUClient(host="https://x.test", cookie="  a=1  ")
        self.assertEqual(c.cookie, "a=1")

    def test_absorbs_set_cookie(self) -> None:
        class H:
            @staticmethod
            def get_all(name):
                return ["route=1790785717.312.28718; Path=/yewu11/; HttpOnly"]

        c = LEYUClient(host="https://x.test")
        c._absorb_cookies(H())
        self.assertIn("route=1790785717.312.28718", c.cookie)

    def test_set_cookie_overrides_same_name(self) -> None:
        class H:
            @staticmethod
            def get_all(name):
                return ["route=NEW; Path=/"]

        c = LEYUClient(host="https://x.test", cookie="route=OLD")
        c._absorb_cookies(H())
        self.assertIn("route=NEW", c.cookie)
        self.assertNotIn("route=OLD", c.cookie)

    def test_set_cookie_without_value_removes(self) -> None:
        class H:
            @staticmethod
            def get_all(name):
                return ["route=; Path=/; Max-Age=0"]

        c = LEYUClient(host="https://x.test", cookie="route=OLD; keep=1")
        c._absorb_cookies(H())
        self.assertNotIn("route", c.cookie)
        self.assertIn("keep=1", c.cookie)

    def test_absent_get_all_is_tolerated(self) -> None:
        class H:  # 没有 get_all 的假 headers
            pass

        c = LEYUClient(host="https://x.test", cookie="a=1")
        c._absorb_cookies(H())  # 不应抛异常
        self.assertEqual(c.cookie, "a=1")

    def test_empty_set_cookie_list_noop(self) -> None:
        class H:
            @staticmethod
            def get_all(name):
                return []

        c = LEYUClient(host="https://x.test", cookie="a=1")
        c._absorb_cookies(H())
        self.assertEqual(c.cookie, "a=1")


# --------------------------------------------------------------------------- #
# 回归：大响应被截断（真实故障 —— 「订阅列表获取失败」）
# --------------------------------------------------------------------------- #

class TestTruncatedResponseRegression(unittest.TestCase):
    """`_read_body` 必须识别截断，并且**重试**而不是把半截数据交出去。

    背景（本项目真实故障）：
        全量赛程 `getOriginalDataPB` 响应体数百 KB，实测反复抛
        `http.client.IncompleteRead`。该异常的 MRO 是
        `IncompleteRead → HTTPException → Exception`，
        **既不继承** `urllib.error.URLError` **也不继承** `OSError`，
        因此 `_request` 的 except 分支完全接不住 → 异常穿透 →
        `mids_provider` 失败 → 实时推送**只订阅到 2 场** →
        “盘口变动触发决策”几乎无信号可触发（用户看到页面没反应）。

    这里用假 response 对象锁定两条不变式：
        1. 声明 Content-Length 但实际读得少 → 必抛 IncompleteRead；
        2. 长度在容差内 → 正常返回，不误伤。
    """

    @staticmethod
    def _resp(chunks, declared):
        class H:
            @staticmethod
            def get(name):
                return declared if name == "Content-Length" else None

        class R:
            headers = H()

            def __init__(self):
                self._it = iter(chunks)

            def read(self, n=-1):
                try:
                    return next(self._it)
                except StopIteration:
                    return b""

        return R()

    def test_short_body_raises_incomplete_read(self) -> None:
        import http.client
        # 声明 1000 字节，只给 100 字节
        r = self._resp([b"x" * 100], "1000")
        with self.assertRaises(http.client.IncompleteRead):
            LEYUClient._read_body(r)

    def test_full_body_ok(self) -> None:
        payload = b"y" * 500
        r = self._resp([payload], "500")
        self.assertEqual(LEYUClient._read_body(r), payload)

    def test_within_tolerance_ok(self) -> None:
        # 声明 1000，实读 995（0.5% 差异）→ 容差内，不报错
        r = self._resp([b"z" * 995], "1000")
        self.assertEqual(len(LEYUClient._read_body(r)), 995)

    def test_no_content_length_is_tolerated(self) -> None:
        r = self._resp([b"a" * 10], None)
        self.assertEqual(LEYUClient._read_body(r), b"a" * 10)
