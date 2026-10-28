"""One format selection for polling, @ intake and delegated grants."""
import json
from pathlib import Path
from .contracts import ContractError

OFFICE_PROFILE = 'office-documents-guide-v1'
OFFICE_FORMATS = ['.md', '.webarchive', '.pdf', '.txt', '.docx', '.xlsx']

def daily_formats(config):
    formats = ['.md', '.webarchive'] + (['.pdf'] if 'pdf' in config else [])
    if 'guide_plan' in config:
        profile = json.loads(Path(config['guide_plan']).read_bytes())
        if profile.get('reference') == OFFICE_PROFILE:
            if profile.get('formats') != OFFICE_FORMATS:
                raise ContractError('office guide requires the exact versioned format list')
            formats += ['.txt', '.docx', '.xlsx']
    return formats

def unsupported_message(config):
    if '.docx' in daily_formats(config):
        return '当前支持 TXT、DOCX、XLSX、Markdown、网页归档及已启用的 PDF。旧版 DOC/XLS 请先另存为 DOCX/XLSX；不接收宏文件。'
    return '当前支持 Markdown、Safari 网页归档和已启用的论文 PDF；新增文档格式尚未部署。'
