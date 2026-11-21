# Self-host short conversation visibility

Set `OMI_SELFHOST_KEEP_SHORT_CONVERSATIONS=true` on backend and pusher and
recreate the services to retain automatically ended conversations under 300
words on Home. Unset, false or any other value preserves upstream's word minimum.

Only the word minimum is bypassed. Media and positively recognized background
remain hidden; uncertain background still follows upstream's conversation
classification. Manual end, empty-audio handling, authentication, memory
extraction and the recorded-silence timeout are unchanged. Visibility does not
promise a task or memory: those still depend on the content and processing.

No historical backfill or blanket unhide is performed. A historical row touched
by ordinary upstream reconciliation may be re-projected under the active
policy; the switch is not a migration or an immutability guarantee.

Conversation timing now uses upstream's native controls. Set
`CONVERSATIONS_V3_SILENCE_GAP_SECONDS=120` and `CONVERSATIONS_V3_LOCK_SECONDS=120`
for the prior two-minute behavior. Cloud Tasks and model work add processing time afterward.

Rollback: remove this visibility flag (or set false) and recreate the services,
or restore the exact pre-rollout image pins and env backup. Keep rollback
images/config until real pendant acceptance is complete.
