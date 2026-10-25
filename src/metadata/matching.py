"""Conservative work matching. A character name alone is never a work identity."""
from urllib.parse import urlparse
import re
import unicodedata


def language(url):
    p=urlparse(url)
    if p.netloc in {'www.miyoushe.com','miyoushe.com','zzz.mihoyo.com'}:return 'zh-cn'
    if p.netloc=='zenless.hoyoverse.com':
        match=re.match(r'/(zh-cn|zh-tw|en-us|ja-jp|ko-kr)/',p.path)
        return match[1] if match else None
    return None


def signature(title):
    title=unicodedata.normalize('NFKC',title)
    title=re.sub(r'^\s*《绝区零》\s*','',title)
    return re.sub(r'[\s|丨｜「」“”"\'·:：]', '', title).casefold()


def match_official(title, rows, lang='zh-cn'):
    key=signature(title)
    candidates=sorted({row['post_url'] for row in rows if row.get('post_url') and
                       urlparse(row['post_url']).netloc in {'zzz.mihoyo.com','zenless.hoyoverse.com'} and
                       language(row['post_url'])==lang and signature(row.get('post_name',''))==key})
    return {'status':'matched' if len(candidates)==1 else 'ambiguous' if candidates else 'unmatched',
            'url':candidates[0] if len(candidates)==1 else None,'candidates':candidates,
            'rule':'exact_normalized_work_title_and_language'}
