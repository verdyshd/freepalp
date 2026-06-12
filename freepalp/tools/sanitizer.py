#!/usr/bin/env python3
"""
敏感信息清洗和扫描工具。

用法:
    # 清洗 JSON 文件中的消息文本
    python3 sanitizer.py clean --input parsed.json --output cleaned.json

    # 扫描目录下所有文件是否有残留敏感信息
    python3 sanitizer.py scan --dir /path/to/workspace

    # 清洗单个文本文件
    python3 sanitizer.py clean-text --input raw.txt --output cleaned.txt
"""

import json
import sys
import re
import argparse
from pathlib import Path

# ============================================================
# 敏感信息过滤规则
# 每条规则: (名称, 正则, 替换文本)
# ============================================================
RULES = [
    # === 身份证件 ===
    ("id_card_cn",
     r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
     "[REDACTED_ID]"),
    ("passport_cn",
     r"(?<![a-zA-Z])[EGDSPH]\d{8}(?!\d)",
     "[REDACTED_PASSPORT]"),
    ("hk_macao_permit",
     r"(?<![a-zA-Z])[CHM]\d{8}(?!\d)",
     "[REDACTED_PERMIT]"),

    # === 联系方式 ===
    ("phone_cn",
     r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)",
     "[REDACTED_PHONE]"),
    ("landline_cn",
     r"(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)",
     "[REDACTED_PHONE]"),
    ("phone_intl",
     r"(?<!\d)\+\d{1,3}[-\s]?\d{6,14}(?!\d)",
     "[REDACTED_PHONE]"),
    ("email",
     r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
     "[REDACTED_EMAIL]"),

    # === 社交账号 ===
    ("wechat_id",
     r"wxid_[a-zA-Z0-9_]{6,}",
     "[REDACTED_WECHAT]"),
    ("qq_number",
     r"(?:QQ|qq)\s*[:：]?\s*\d{5,12}",
     "[REDACTED_QQ]"),

    # === 金融信息 ===
    ("bank_card",
     r"(?<!\d)[3-6]\d{15,18}(?!\d)",
     "[REDACTED_CARD]"),
    ("cvv",
     r"(?i)(?:cvv|cvc|安全码)\s*[:：]?\s*\d{3,4}",
     "[REDACTED_CVV]"),

    # === 车辆信息 ===
    ("plate_cn",
     r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]",
     "[REDACTED_PLATE]"),

    # === 位置信息 ===
    ("gps_coords",
     r"(?i)(?:经度|纬度|lat|lng|longitude|latitude)\s*[:：]?\s*-?\d{1,3}\.\d{4,}",
     "[REDACTED_GPS]"),
    ("address_cn",
     r"[\u4e00-\u9fa5]{2,}(?:省|自治区)[\u4e00-\u9fa5]{2,}(?:市|州|盟)[\u4e00-\u9fa5]{2,}(?:区|县|旗|市)[\u4e00-\u9fa5\d]*(?:路|街|道|巷|弄|里|村|镇)[\u4e00-\u9fa5\d]*号?",
     "[REDACTED_ADDRESS]"),
    ("zipcode_cn",
     r"(?i)(?:邮编|邮政编码|zip)\s*[:：]?\s*\d{6}",
     "[REDACTED_ZIPCODE]"),

    # === 日程行踪 ===
    ("flight_cn",
     r"(?<![a-zA-Z])(?:CA|MU|CZ|HU|ZH|MF|SC|3U|FM|KN|GS|BK|EU|JD|QW|TV|PN|DR|GJ|AQ|9C|G5)\d{3,4}(?!\d)",
     "[REDACTED_FLIGHT]"),
    ("train_cn",
     r"(?<![a-zA-Z0-9\-:])[GCDZTSPKYL]\d{1,4}(?!\d)",
     "[REDACTED_TRAIN]"),

    # === 人名 ===
    # @提及的人名（中英文）
    ("name_at_mention",
     r"@[\u4e00-\u9fa5]{2,4}|@[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}",
     "[REDACTED_NAME]"),
    # 带上下文线索的中文姓名（"跟张三说"、"让李明来"、"找王总"等）
    ("name_cn_context",
     r"(?:跟|和|与|给|叫|找|问|让|请|被|叫做|名叫|联系|通知|转告|告诉)\s*"
     + r"[\u4e00-\u9fa5]{2,4}(?=\s|$|[，。！？,!?\s说来的了吧呢吗])",
     "[REDACTED_NAME]"),
    # 英文全名（两到三个大写开头单词，排除常见非人名词汇）
    ("name_en_full",
     r"(?<![A-Za-z])(?!(?:The|This|That|What|When|Where|Which|How|And|But|For|Not)\b)"
     + r"[A-Z][a-z]{1,15}\s+[A-Z][a-z]{1,15}(?:\s+[A-Z][a-z]{1,15})?"
     + r"(?![A-Za-z])",
     "[REDACTED_NAME]"),
    # 中文姓名 + 职位/称呼后缀（"张总"、"李老师"、"王哥"等）
    ("name_cn_title",
     r"[\u4e00-\u9fa5]{1,2}(?:总|经理|主任|老师|教授|博士|医生|律师|老板|哥|姐|叔|阿姨|弟|妹|兄|嫂)",
     "[REDACTED_NAME]"),

    # === 凭证 & Token ===
    ("credential",
     r"(?i)(?:api[_-]?key|token|secret|password|passwd|app[_-]?secret|access[_-]?key)\s*[:=]\s*[\"']?[a-zA-Z0-9_\-\.]{16,}[\"']?",
     "[REDACTED_CREDENTIAL]"),
    ("private_key",
     r"-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE\s+KEY|CERTIFICATE)-----[\s\S]*?-----END\s+(?:RSA\s+)?(?:PRIVATE\s+KEY|CERTIFICATE)-----",
     "[REDACTED_KEY]"),

    # === 网络信息 ===
    ("ip_address",
     r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d{1,5})?\b",
     "[REDACTED_IP]"),
    ("internal_url",
     r"(?i)https?://(?:localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)[^\s)\"']*",
     "[REDACTED_INTERNAL_URL]"),
    ("ws_url",
     r"(?i)wss?://[^\s)\"']+",
     "[REDACTED_WS_URL]"),
    ("db_url",
     r"(?i)(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis|mssql)://[^\s)\"']+",
     "[REDACTED_DB_URL]"),
    ("ssh_cmd",
     r"(?i)ssh\s+(?:-[a-zA-Z]\s+)*\S+@\S+",
     "[REDACTED_SSH]"),

    # === 文件路径 & 标识符 ===
    ("abs_path",
     r"(?:/(?:Users|home|root)/[a-zA-Z0-9_\-]+|[A-Z]:\\Users\\[a-zA-Z0-9_\-]+)(?:[/\\][^\s)\"']*)",
     "[REDACTED_PATH]"),
    ("uuid",
     r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
     "[REDACTED_UUID]"),
    ("hex_token",
     r"(?<![0-9a-fA-F])[0-9a-f]{32,}(?![0-9a-fA-F])",
     "[REDACTED_HEX]"),
]


def clean_text(text):
    """清洗文本，返回 (cleaned_text, report_list)。"""
    report = []
    for name, pattern, replacement in RULES:
        matches = list(re.finditer(pattern, text))
        if matches:
            report.append({
                'rule': name,
                'count': len(matches),
                'samples': [m.group()[:20] + '...' for m in matches[:3]]
            })
            text = re.sub(pattern, replacement, text)
    return text, report


def scan_text(text):
    """扫描文本，返回违规列表。"""
    violations = []
    for name, pattern, _ in RULES:
        matches = list(re.finditer(pattern, text))
        for m in matches:
            violations.append({
                'rule': name,
                'snippet': m.group()[:30]
            })
    return violations


def cmd_clean(args):
    """清洗 parsed.json 中的消息文本。"""
    data = json.loads(Path(args.input).read_text(encoding='utf-8'))
    total_report = []

    for msg in data.get('messages', []):
        if 'text' in msg:
            cleaned, report = clean_text(msg['text'])
            msg['text'] = cleaned
            total_report.extend(report)

    Path(args.output).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )

    if total_report:
        print("清洗报告:", file=sys.stderr)
        for item in total_report:
            print(f"  [{item['rule']}] {item['count']} 处匹配", file=sys.stderr)
    else:
        print("未发现格式化敏感信息", file=sys.stderr)


def cmd_clean_text(args):
    """清洗单个文本文件。"""
    text = Path(args.input).read_text(encoding='utf-8')
    cleaned, report = clean_text(text)
    Path(args.output).write_text(cleaned, encoding='utf-8')

    if report:
        print("清洗报告:", file=sys.stderr)
        for item in report:
            print(f"  [{item['rule']}] {item['count']} 处匹配", file=sys.stderr)


def cmd_scan(args):
    """扫描目录下所有文件。"""
    scan_dir = Path(args.dir)
    if not scan_dir.exists():
        print(f"错误: 目录不存在: {args.dir}", file=sys.stderr)
        sys.exit(1)

    all_violations = {}
    for filepath in scan_dir.rglob('*'):
        if not filepath.is_file():
            continue
        if filepath.suffix in ('.json', '.md', '.txt', '.yaml', '.yml'):
            try:
                text = filepath.read_text(encoding='utf-8')
            except (UnicodeDecodeError, PermissionError):
                continue
            violations = scan_text(text)
            if violations:
                rel = str(filepath.relative_to(scan_dir))
                all_violations[rel] = violations

    if all_violations:
        print("⚠️  发现残留敏感信息:", file=sys.stderr)
        files_detail: dict[str, list[dict[str, str]]] = {}
        for filename, violations in all_violations.items():
            print(f"\n  {filename}:", file=sys.stderr)
            files_detail[filename] = []
            for v in violations:
                print(f"    [{v['rule']}] {v['snippet']}", file=sys.stderr)
                files_detail[filename].append(v)
        print(json.dumps({'safe': False, 'files': files_detail}, ensure_ascii=False))
        sys.exit(1)
    else:
        print("✅ 所有文件安全，未发现残留敏感信息", file=sys.stderr)
        print(json.dumps({'safe': True, 'files': {}}))
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description='敏感信息清洗和扫描工具')
    sub = parser.add_subparsers(dest='command')

    p_clean = sub.add_parser('clean', help='清洗 parsed.json')
    p_clean.add_argument('--input', required=True)
    p_clean.add_argument('--output', required=True)

    p_text = sub.add_parser('clean-text', help='清洗单个文本文件')
    p_text.add_argument('--input', required=True)
    p_text.add_argument('--output', required=True)

    p_scan = sub.add_parser('scan', help='扫描目录')
    p_scan.add_argument('--dir', required=True)

    args = parser.parse_args()

    if args.command == 'clean':
        cmd_clean(args)
    elif args.command == 'clean-text':
        cmd_clean_text(args)
    elif args.command == 'scan':
        cmd_scan(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
