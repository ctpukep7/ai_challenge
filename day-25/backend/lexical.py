"""Bounded BM25 context supplements from already anchored README files.

The persistent index is opened read-only. Lexical scores are never reported as
cosine scores; a weak cosine search cannot be rescued by this expansion.
"""
from __future__ import annotations
from collections import Counter
import math
from pathlib import Path
import re
import sqlite3

STOP = set('day день дня дне дней в во на по из за к ко с со у о об от до для при и или а но не ни без это этот эта эти то что как какие какой какая когда где ли же бы можно нужно надо мне меня мы вы он она они его её их чем чем-то после перед также только теперь есть будет будут работает работать происходит вопрос вопроса отправляет обойтись'.split())

def tokens(text):
    words=[word for word in re.findall(r'[^\W_]+',text.casefold(),re.UNICODE) if len(word)>1 and not word.isdigit() and word not in STOP]
    # A small deterministic inflection normalizer joins сообщения/сообщений and
    # similar Russian endings without changing technical identifiers.
    return [re.sub(r'(иями|ями|ами|ого|ему|ыми|ими|ия|ий|ие|ии|ию|ям|ах|ях|ам|ом|ем|ой|ый|ая|ое|ую|ы|и|а|я|у|ю|е|о)$','',word) if re.fullmatch(r'[а-яё]{5,}',word) else word for word in words]


def expand_context(question, strategy, anchors, evidence, *, db):
    if not anchors: return []
    sources=sorted({c['source'] for c in anchors})
    query=list(dict.fromkeys(tokens(question)))[:128]
    if not query: return []
    conn=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True)
    conn.row_factory=sqlite3.Row
    try:
        rows=[dict(row) for row in conn.execute(
            'SELECT strategy,chunk_id,source,title,section,ordinal,text,token_count,overlap_tokens,crosses_sections FROM chunks WHERE strategy=? AND source IN ('+','.join('?' for _ in sources)+') ORDER BY source,ordinal', [strategy,*sources])]
    finally: conn.close()
    if not rows: return []
    frequencies=[Counter(tokens(row['text'])) for row in rows]
    lengths=[sum(freq.values()) for freq in frequencies]
    average=sum(lengths)/len(rows) or 1
    df={word:sum(word in freq for freq in frequencies) for word in query}
    disclosed={c['chunk_id'] for c in evidence}
    ranked=[]
    for row,freq,length in zip(rows,frequencies,lengths):
        if row['chunk_id'] in disclosed: continue
        matches=[word for word in query if word in freq]
        # Two meaningful words, or a rare technical identifier. Generic Russian
        # overlap alone is insufficient; day numbers and question boilerplate drop out.
        rare_technical=any(re.fullmatch(r'[a-z][a-z0-9]*',word) and len(word)>=3 and df[word]<=max(1,len(rows)*.15) for word in matches)
        if len(matches)<2 and not rare_technical: continue
        score=sum(math.log(1+(len(rows)-df[word]+.5)/(df[word]+.5))*
            freq[word]*2.2/(freq[word]+1.2*(.25+.75*length/average)) for word in matches)
        if score>0: ranked.append((score,row))
    ranked.sort(key=lambda item:(-item[0],item[1]['source'],item[1]['ordinal'],item[1]['chunk_id']))
    result=[]
    for _,row in ranked[:2]:
        anchor=next(c for c in anchors if c['source']==row['source'])
        result.append({**row,'score':None,'retrieval_role':'lexical_context','anchor_chunk_id':anchor['chunk_id']})
    return result
