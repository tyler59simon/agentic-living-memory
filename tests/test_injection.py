"""The prompt hook's injection controller (core/injection.py): levels, follow-ups, acknowledgments, section-level dedupe
and the one budget, through the real hook path. Lexical only unless a test installs the tiny model."""
from unittest.mock import patch
from core import injection,runtime,semantic
from core.notes import capture
from test_core import StoreFixture

TAGS=['subject/project','concern/decision']
DECISION='''Event delivery uses the outbox table polled by the dispatcher.

## Why
Polling the outbox keeps delivery inside the database transaction, so no message is lost when the dispatcher restarts.

## Alternatives considered
- Kafka streaming: rejected because running a broker cluster costs more than the volume needs.
- Direct webhooks from request handlers: rejected because a failed call loses the event.
'''
RUNBOOK='''Rotate the signing keys every quarter.

## Steps
1. Run `keyctl rotate --all --confirm` on the vault host.
2. Restart the gateway with `svc restart gateway`.
3. Check `keyctl status` shows the new key id.
'''

class Delivery(StoreFixture):
    def setUp(self):
        super().setUp()
        self.decision=capture('Event delivery design',DECISION,TAGS,'demo',self.root,type='decision')
    def prompt(self,text,session='s'):
        payload={'cwd':str(self.base/'project'),'session_id':session,'prompt':text}
        return runtime.process('claude','user-prompt',payload,self.root)['context']
    def start(self,session='s',source='startup'):
        runtime.process('claude','session-start',{'cwd':str(self.base/'project'),'session_id':session,'source':source},self.root)

    def test_a_section_question_gets_that_section_as_evidence(self):
        text=self.prompt('why do we poll the outbox dispatcher transaction')
        self.assertIn('Event delivery design — Event delivery uses the outbox table polled by the dispatcher. § Why',text)   # the summary leads the section
        self.assertIn('no message is lost',text)
        self.assertRegex(text,r'§ Why \(lines \d+-\d+\)')

    def test_a_rejected_option_is_labelled_and_never_shown_as_the_choice(self):
        text=self.prompt('kafka streaming broker cluster')
        self.assertIn('§ Alternatives considered [rejected or considered options, not the decision]',text)

    def test_a_general_question_gets_the_pointer_line(self):
        text=self.prompt('event delivery design')
        line=next(l for l in text.splitlines() if 'Event delivery design' in l)
        self.assertNotIn('§',line);self.assertIn(' — ',line)

    def test_acknowledgments_retrieve_nothing(self):
        for text in ('thanks!','ok, go ahead','LGTM','sounds good.'):
            self.assertNotIn('Event delivery',self.prompt(text,'ack-'+text[:3]))

    def test_a_short_follow_up_answers_from_the_previous_topic(self):
        self.prompt('event delivery design')
        text=self.prompt('why?')
        self.assertIn('§ Why',text);self.assertIn('no message is lost',text)
        text=self.prompt('what were the alternatives?')
        self.assertIn('§ Alternatives considered',text)

    def test_a_follow_up_with_no_previous_topic_is_quiet(self):
        self.assertNotIn('Event delivery',self.prompt('why?','fresh'))

    def test_the_same_section_is_not_repeated_but_another_section_is_delivered(self):
        first=self.prompt('why do we poll the outbox dispatcher transaction')
        self.assertIn('§ Why',first)
        again=self.prompt('outbox dispatcher transaction polling')
        self.assertNotIn('§ Why',again)
        other=self.prompt('kafka streaming broker cluster rejected')
        self.assertIn('§ Alternatives considered',other)

    def test_an_explicit_request_and_a_new_revision_deliver_again(self):
        self.prompt('why do we poll the outbox dispatcher transaction')
        self.assertIn('§ Why',self.prompt('remind me why we poll the outbox dispatcher transaction'))
        path=next((self.root/'domains/demo/notes').glob('*event-delivery*'))
        path.write_text(path.read_text(encoding='utf-8').replace('dispatcher restarts','dispatcher restarts or crashes'),encoding='utf-8')
        from core.search import rebuild_index
        rebuild_index('demo',self.root)
        self.assertIn('§ Why',self.prompt('why do we poll the outbox dispatcher transaction'))

    def test_a_new_session_or_compaction_forgets_what_was_delivered(self):
        self.prompt('why do we poll the outbox dispatcher transaction')
        self.start(source='compact')
        self.assertIn('§ Why',self.prompt('why do we poll the outbox dispatcher transaction'))

    def test_asking_for_detail_expands_the_section(self):
        capture('Signing key rotation',RUNBOOK,TAGS,'demo',self.root,type='runbook',allow_similar=True)
        text=self.prompt('show me the exact steps to rotate signing keys with keyctl')
        self.assertIn('keyctl status',text)   # the last step: the section was not cut to the evidence width
        self.assertIn('§ Steps',text)

    def test_the_budget_keeps_the_first_hit_and_drops_later_ones(self):
        for i in range(4):capture(f'Outbox fact {i}','Outbox dispatcher transaction detail. '*40,TAGS,'demo',self.root,allow_similar=True)
        with patch.object(injection,'PROMPT_CAP',300):
            text=self.prompt('outbox dispatcher transaction detail')
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith('- [')),1)

    def test_within_never_drops_the_first_line(self):
        self.assertEqual(injection.within(['x'*500,'y'],'h',10),1)
        self.assertEqual(injection.within(['a','b','c'],'h',100),3)

    def test_the_line_format_before_units_is_one_switch_away(self):
        with patch.object(runtime,'DELIVERY',False):
            text=self.prompt('why do we poll the outbox dispatcher transaction','legacy')
        self.assertIn('Event delivery design — ',text);self.assertNotIn('§',text)

    def test_cuts_are_at_sentence_ends(self):
        text=injection._cut('First sentence is here. Second sentence goes on and on and on.',40)
        self.assertEqual(text,'First sentence is here. …')

class Classifiers(StoreFixture):
    def test_follow_up_roles(self):
        self.assertEqual(injection.follow_up('why?',set()),'rationale')
        self.assertEqual(injection.follow_up('what was the alternative?',{'alternative'}),'alternative')
        self.assertEqual(injection.follow_up('does that still apply?',{'apply'}),'overview')
        self.assertIsNone(injection.follow_up('why does the gateway cache keys for an hour',{'gateway','cache','keys','hour'}))
        self.assertIsNone(injection.follow_up('ok',set()))

    def test_acks(self):
        for text in ('yes','Thanks!','ok go ahead','lgtm','sounds good, proceed'):self.assertTrue(injection.is_ack(text),text)
        for text in ('yes, but why the outbox?','ok so what about kafka','no'):self.assertFalse(injection.is_ack(text),text)
