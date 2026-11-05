"""Recent work remains discoverable as a user's conversation history grows."""
from uuid import uuid4

from sqlalchemy import text


def test_recent_directory_and_oldest_pending_work(store):
    with store.transaction() as conn:
        for index in range(105):
            conn.execute(text("""INSERT INTO agent.sessions
                (tenant_id,workspace_id,id,owner,revision,state,updated_at)
                VALUES (:tenant,:workspace,:id,'alice',1,
                    jsonb_build_object('title',CAST(:title AS text),'status','queued'),
                    timestamp '2026-01-01' + :offset * interval '1 second')"""),
                {**store.scope,'id':str(uuid4()),'title':f'Run {index}','offset':index})
    recent=store.list('alice',viewer='alice')
    assert len(recent)==100
    assert recent[0]['title']=='Run 104' and recent[-1]['title']=='Run 5'
    pending=store.list(pending=True)
    assert pending[0]['title']=='Run 0' and pending[-1]['title']=='Run 99'
    assert store.list('bob',viewer='bob')==[]
