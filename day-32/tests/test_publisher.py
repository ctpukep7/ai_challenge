import asyncio
import copy
import json

import httpx
import pytest

from day32_review.fixtures import FixtureGitHub
from day32_review.publisher import canonical_key, identity, marker, threads
from day32_review.review import GitHub, publish


def run(value):
    return asyncio.run(value)


def finding(**changes):
    return dict(dict(category='bug',severity='P2',title='Wrong value',explanation='Use the documented value.',
        file='app.py',side='head',line=1,evidence='new',hypothesis=False),**changes)


def state(**changes):
    value = {'repo':'owner/repo','pr':{'base':{'sha':'b'*40},'head':{'sha':'a'*40,'repo':{'full_name':'owner/repo'}},'changed_files':1},
             'files':[{'filename':'app.py','status':'modified','patch':'@@ -1 +1 @@\n-old\n+new'}]}
    value.update(changes)
    return FixtureGitHub(value)


def report(**changes):
    value = dict(repo='owner/repo',mode='fixture',number=1,base_sha='b'*40,head_sha='a'*40,status='reviewed',
                 findings=[finding()],reviewed_files=['app.py'],total_files=1,sources=[],partial=False,rejected_findings=0,skipped=[])
    value.update(changes)
    return value


def test_real_right_payload_summary_and_repeat():
    api=state(); result=run(publish(api,report()))
    post=api.mutations[0]
    assert post['path']=='pulls/1/comments'
    assert {k:post['body'][k] for k in ('path','side','line','commit_id')} == dict(path='app.py',side='RIGHT',line=1,commit_id='a'*40)
    assert result['created']==1 and result['inline'][0]['url']
    issue=api.comments[0]['body']
    assert 'Wrong value' not in issue and 'Files changed' in issue
    result=run(publish(api,report(findings=[finding(title='New wording')])));
    assert result['created']==0 and result['updated']==1
    assert len(api.inline)==1 and len(api.comments)==1


@pytest.mark.parametrize('status,path,previous', [('renamed','new.py','old.py'),('removed','app.py','app.py')])
def test_left_rename_delete_uses_current_diff_path(status,path,previous):
    api=state(files=[dict(filename=path,previous_filename=previous,status=status,patch='@@ -1 +1 @@\n-old\n+new')])
    result=run(publish(api,report(findings=[finding(file=path,side='base',evidence='old')],reviewed_files=[path])))
    assert api.inline[0]['path']==path and api.inline[0]['side']=='LEFT'
    assert result['created']==1


def test_key_is_exact_and_independent_of_wording_and_line_number():
    assert canonical_key('app.py','head','  new') != canonical_key('app.py','head','new')
    api=state(); run(publish(api,report())); original=identity(api.inline[0]['body'])[0]
    api.data['files'][0]['patch']='@@ -8 +8 @@\n-old\n+new'
    api.inline[0]['line']=8;api.threads[0]['line']=8
    run(publish(api,report(findings=[finding(line=8,title='Another title')])))
    assert len(api.inline)==1 and identity(api.inline[0]['body'])[0]==original


def test_same_line_merges_but_identical_code_on_distinct_lines_does_not():
    api=state(files=[dict(filename='app.py',status='modified',patch='@@ -1 +1,2 @@\n-old\n+new\n+new')])
    result=run(publish(api,report(findings=[finding(),finding(title='Second concern'),finding(line=2)])))
    assert result['created']==2 and len(api.inline)==2
    assert 'Second concern' in api.inline[0]['body']
    run(publish(api,report(findings=[finding(),finding(line=2)])))
    assert len(api.inline)==2


def test_bad_current_quote_aborts_before_mutation():
    api=state()
    with pytest.raises(ValueError,match='подтверждена'):
        run(publish(api,report(findings=[finding(),finding(evidence='imagined')])));
    assert not api.mutations


def test_human_spoof_and_reply_not_updated():
    api=state();run(publish(api,report()))
    root=api.threads[0]['comments']['nodes'][0]
    root['author']={'login':'github-actions','__typename':'User'}
    api.inline[0]['user']={'login':'github-actions[bot]','type':'User'}
    run(publish(api,report())); assert len(api.inline)==2
    api.inline[-1]['in_reply_to_id']=1
    run(publish(api,report())); assert len(api.inline)==3


def test_human_resolve_preserved_but_bot_resolve_reopened():
    api=state();run(publish(api,report()))
    thread=api.threads[0];thread['isResolved']=True
    thread['resolvedBy']={'login':'human','__typename':'User'}
    before=len(api.mutations)
    result=run(publish(api,report()))
    assert result['reopened']==0 and thread['isResolved']
    assert not any(m.get('path','').startswith('pulls/comments/') for m in api.mutations[before:])
    thread['resolvedBy']={'login':'github-actions','__typename':'Bot'}
    result=run(publish(api,report())); assert result['reopened']==1 and not thread['isResolved']


def old_thread(api):
    run(publish(api,report()));api.threads[0]['isOutdated']=True


@pytest.mark.parametrize('changes', [dict(partial=True,rejected_findings=1),dict(partial=True,skipped=[{'path':'app.py','reason':'truncated'}]),dict(partial=True,skipped=[{'path':'other.py','reason':'not analyzed'}])])
def test_uncertain_analysis_does_not_resolve(changes):
    api=state();old_thread(api)
    api.data['files'][0]['patch']='@@ -1 +1 @@\n-old\n+fixed'
    result=run(publish(api,report(findings=[],**changes)))
    assert result['resolved']==0


def test_empty_model_answer_does_not_resolve_existing_anchor():
    api=state();old_thread(api)
    assert run(publish(api,report(findings=[])))['resolved']==0


def test_exact_anchor_disappeared_complete_analysis_can_close():
    api=state();old_thread(api);api.data['files'][0]['patch']='@@ -1 +1 @@\n-old\n+fixed'
    result=run(publish(api,report(findings=[],partial=True,skipped=[{'path':'(base RAG)','reason':'1MB'}])))
    assert result['resolved']==1 and api.threads[0]['isResolved']
    assert 'Это не доказательство исправления' in api.comments[0]['body']


def test_absent_file_revert_closes_only_full_files_api():
    api=state();old_thread(api);api.data['files']=[]
    assert run(publish(api,report(findings=[])))['resolved']==0 # count still 1 => incomplete
    api.data['pr']['changed_files']=0
    assert run(publish(api,report(findings=[],partial=True,skipped=[{'path':'other.py','reason':'budget'}])))['resolved']==1


def test_outdated_matching_finding_new_thread_and_no_auto_close():
    api=state();old_thread(api)
    result=run(publish(api,report()))
    assert result['created']==1 and result['resolved']==0
    assert len(api.inline)==2


def test_partial_failure_retry_does_not_duplicate_inline():
    api=state();original=api.request
    async def fail_summary(method,path,**kw):
        if method=='POST' and path.startswith('issues/'):
            raise ValueError('simulated publication failure')
        return await original(method,path,**kw)
    api.request=fail_summary
    with pytest.raises(ValueError):run(publish(api,report()))
    assert len(api.inline)==1
    api.request=original
    assert run(publish(api,report()))['created']==0
    assert len(api.inline)==1


def test_sha_changes_before_inline_mutation_abort():
    api=state();original=api.request;reads=0
    async def race(method,path,**kw):
        nonlocal reads
        if method=='GET':
            reads+=1
            if reads==3:api.data['pr']['head']['sha']='c'*40
        return await original(method,path,**kw)
    api.request=race
    with pytest.raises(ValueError,match='Устаревший'):run(publish(api,report()))
    assert not api.mutations


def test_anchor_limit_explicit():
    patch='@@ -1,0 +1,31 @@\n'+'\n'.join('+new'+str(i) for i in range(31))
    api=state(files=[dict(filename='app.py',status='added',patch=patch)])
    result=run(publish(api,report(findings=[finding(line=i+1,evidence='new'+str(i)) for i in range(31)])))
    assert result['created']==30 and result['omitted_anchors']==1
    assert 'лимита: 1' in api.comments[0]['body']


def test_hostile_text_cannot_ping_or_inject_markdown():
    api=state();run(publish(api,report(findings=[finding(title='@all <b> [x](https://evil.example)')])));
    text=api.inline[0]['body']
    assert '@all' not in text and '<b>' not in text and '[x](https://evil.example)' not in text


def test_graphql_pagination_errors_fail_closed():
    calls=[]
    def handle(request):
        payload=json.loads(request.content);calls.append(payload)
        cursor=payload['variables']['cursor']
        return httpx.Response(200,json={'data':{'repository':{'pullRequest':{'reviewThreads':{'nodes':[], 'pageInfo':{'hasNextPage':cursor is None,'endCursor':'next'}}}}}})
    api=GitHub('owner/repo','fake',transport=httpx.MockTransport(handle))
    assert run(threads(api,1))==[] and len(calls)==2
    assert calls[1]['variables']['cursor']=='next'
    def broken(request):return httpx.Response(200,json={'data':{},'errors':[{'message':'denied'}]})
    bad=GitHub('owner/repo','fake',transport=httpx.MockTransport(broken))
    with pytest.raises(ValueError,match='GraphQL'):run(threads(bad,1))


def test_incomplete_threads_abort_before_mutation():
    api=state();api.threads=[{'id':'x'}]
    with pytest.raises(ValueError,match='Неполный'):run(publish(api,report()))
    assert not api.mutations


def test_no_permission_marks_outdated_idempotently_preserves_history():
    api=state();old_thread(api);api.data['files']=[];api.data['pr']['changed_files']=0
    api.threads[0]['viewerCanResolve']=False
    original=api.inline[0]['body']
    result=run(publish(api,report(findings=[])))
    assert result['resolved']==0 and result['outdated_marked']==1
    assert result['lifecycle_skipped'][0]['reason']=='viewer cannot resolve'
    assert api.inline[0]['body'].startswith(original)
    assert identity(api.inline[0]['body'])==identity(original)
    assert 'Это не доказательство исправления' in api.inline[0]['body']
    assert not api.threads[0]['isResolved']
    before=len(api.mutations)
    result=run(publish(api,report(findings=[])))
    assert result['outdated_marked']==1 and not result['lifecycle_skipped'][0]['notice_updated']
    assert not any(m.get('path','').startswith('pulls/comments/') for m in api.mutations[before:])


def test_integration_permission_failure_falls_back_other_errors_fail():
    from day32_review.review import GraphQLPermissionError
    for error, expected in [(GraphQLPermissionError('denied'),True),(ValueError('unknown response'),False)]:
        api=state();old_thread(api);api.data['files']=[];api.data['pr']['changed_files']=0
        original=api.graphql
        async def fail_mutation(query,variables):
            if query.startswith('mutation'):raise error
            return await original(query,variables)
        api.graphql=fail_mutation
        if expected:
            result=run(publish(api,report(findings=[])))
            assert result['outdated_marked']==1 and result['resolved']==0
            assert result['lifecycle_skipped'][0]['reason']=='integration cannot resolve'
        else:
            with pytest.raises(ValueError,match='unknown response'):run(publish(api,report(findings=[])))


def test_no_permission_does_not_mark_human_or_manually_resolved():
    for human in (True,False):
        api=state();old_thread(api);api.data['files']=[];api.data['pr']['changed_files']=0
        api.threads[0]['viewerCanResolve']=False
        if human:
            api.threads[0]['comments']['nodes'][0]['author']={'login':'someone','__typename':'User'}
        else:
            api.threads[0]['isResolved']=True
            api.threads[0]['resolvedBy']={'login':'someone','__typename':'User'}
        assert run(publish(api,report(findings=[])))['outdated_marked']==0


def test_permission_error_requires_exact_integration_forbidden():
    from day32_review.review import GraphQLPermissionError
    for error, expected in [({'type':'FORBIDDEN','message':'Resource not accessible by integration'},GraphQLPermissionError),
                            ({'type':'FORBIDDEN','message':'other permission problem'},ValueError),
                            ({'type':'INTERNAL','message':'Resource not accessible by integration'},ValueError)]:
        def handle(request):return httpx.Response(200,json={'data':None,'errors':[error]})
        api=GitHub('owner/repo','fake',transport=httpx.MockTransport(handle))
        with pytest.raises(expected) as caught:
            run(api.graphql('mutation{resolveReviewThread(input:{threadId:"x"}){thread{id}}}',{}))
        assert type(caught.value) is expected


def test_bot_closed_no_unresolve_permission_warns_without_false_success():
    api=state();run(publish(api,report()))
    api.threads[0].update(isResolved=True,viewerCanUnresolve=False,resolvedBy={'login':'github-actions','__typename':'Bot'})
    before=api.inline[0]['body']
    result=run(publish(api,report()))
    assert result['reopened']==0 and result['updated']==0
    assert result['lifecycle_skipped'][0]['reason']=='viewer cannot unresolve'
    assert api.inline[0]['body']==before and api.threads[0]['isResolved']
