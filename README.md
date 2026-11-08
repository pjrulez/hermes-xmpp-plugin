# Hermes XMPP/Jabber plugin

Third-party XMPP/Jabber gateway platform plugin for Hermes Agent.

See [voice messages](docs/voice-messages.md) for audio transport and STT,
and [MUC sender authorization](docs/muc-authorization.md) for room permissions
and migration from room-only authorization.

This packages the work from the upstream Hermes Agent XMPP PRs as an optional
user plugin so self-hosters can use XMPP now instead of waiting for the core PR
to land. It supports:

- 1:1 XMPP chats
- MUC group rooms
- mandatory STARTTLS to the XMPP server
- XEP-0085 typing indicators
- XEP-0363 HTTP file upload for media/files
- XEP-0004 data forms (e.g., clarify prompts)
- XEP-0050 ad-hoc commands
- XEP-0394 message markup (bold, code, lists)
- XEP-0461 threaded message replies
- XEP-0444 message reactions (e.g., 👀/✅/❌ for processing status)
- XEP-0447 stateless file sharing for voice messages
- OMEMO end-to-end encryption (optional, needs slixmpp-omemo)
- cron and `send_message` delivery through a standalone sender hook
- Hermes platform plugin registration via `ctx.register_platform(...)`

Traffic is encrypted to your XMPP server with TLS. When OMEMO is enabled and
slixmpp-omemo is installed, 1:1 messages are also end-to-end encrypted so the
server cannot read content. MUC OMEMO support depends on client/device
availability.

## Credits

Most adapter code is derived from Eric Lars Lee's upstream PR #17469. Mibay's
PR #3105 is credited for earlier XMPP/OMEMO exploration. See `ATTRIBUTION.md`
for the full credit note. This repo exists because waiting is annoying, not
because the packager wants credit for other people's work. Tiny open-source goblin
energy, responsibly attributed.

## Install

For managed Hermes installations, use supported plugin/package management;
do not run direct pip/uv commands against the managed runtime. Preserve the
existing enabled-plugin list.

From the Hermes profile you want to use:

```bash
cd ~/.hermes/plugins
git clone https://github.com/fastfinge/hermes-xmpp-plugin.git
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python -r ~/.hermes/plugins/hermes-xmpp-plugin/requirements.txt
hermes config set plugins.enabled '["hermes-xmpp-plugin"]'
```

If you run Hermes from a different checkout/venv, install requirements into that
Python environment instead.

Restart the gateway after installing or changing env vars:

```bash
hermes gateway restart
```

## Configure with env vars

Add these to `~/.hermes/.env` for the active profile:

```env
XMPP_JID=hermes@example.org
XMPP_PASSWORD=your-password
XMPP_ALLOWED_USERS=sam@example.org
# Optional:
XMPP_HOST=example.org
XMPP_PORT=5222
# XMPP_DIRECT_TLS=true   # force XEP-0368 direct TLS; auto-on when XMPP_PORT=5223
# XMPP_USE_IPV6=false    # disable IPv6 (default: auto-detect a working global v6 route)
# XMPP_CONNECT_TIMEOUT_SECS=240  # default 180s; raise further on a slow/high-latency link
XMPP_MUC_ROOMS=room@conference.example.org/hermes
XMPP_MUC_NICK=hermes
XMPP_HOME_CHANNEL=sam@example.org
```

`XMPP_ALLOWED_USERS` uses personal bare JIDs. MUC requires both a configured
room and verified personal sender in `muc_allowed_users` (defaults to
`allowed_users`). Unknown identities are denied. `XMPP_ALLOW_ALL_USERS=true`
bypasses DM checks only; it never bypasses MUC authorization.

## Configure with config.yaml

You can also use config.yaml:

```yaml
plugins:
  enabled:
    - hermes-xmpp-plugin

xmpp:
  enabled: true
  jid: hermes@example.org
  password: ${XMPP_PASSWORD}
  allowed_users:
    - sam@example.org
  muc_rooms: "room@conference.example.org/hermes"
  home_channel: sam@example.org
```

Env vars win when both are set.

## OMEMO end-to-end encryption (optional)

Install the extra dependencies:

```bash
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python slixmpp-omemo omemo
```

Then set in `~/.hermes/.env`:

```env
XMPP_OMEMO_ENABLED=true
# Optional — defaults to ~/.hermes/xmpp_omemo.json
XMPP_OMEMO_STORAGE_PATH=/home/fastfinge/.hermes/xmpp_omemo.json
```

Or in `config.yaml`:

```yaml
xmpp:
  omemo_enabled: true
  omemo_storage_path: /home/fastfinge/.hermes/xmpp_omemo.json
```

On first connect, the adapter generates an OMEMO device key and publishes it to
the server. This may take a few seconds — the adapter waits for initialization
before sending encrypted messages. If the recipient hasn't published device keys
yet, messages fall back to plaintext (with a log warning).

Before encrypting to a contact the adapter downloads their OMEMO device list (at most
once per 10 minutes). Without this, a fresh account that has not yet heard from the contact
fails with "NoEligibleDevices" even when presence is mutual.

To skip OMEMO for specific peers, such as another agent on your own trusted server, set
`xmpp.plaintext_peers` (or `XMPP_PLAINTEXT_PEERS`) to a list of bare JIDs. This lowers
security for those peers only; it is off by default.

**Keeping the keys safe.** The key file holds your private OMEMO identity and every session. The
adapter protects it:

- one process per key file: the file is locked while the gateway runs, and a second process that
  opens the same file (a second gateway on the same home, a script) gets OMEMO switched off with a
  log line instead of overwriting the sessions; the one-shot sender (`send_xmpp_message`, used by
  `hermes send` and cron delivery from the CLI) never uses OMEMO for the same reason;
- writes are atomic (temp file, fsync, rename) and the file is owner-only (`0600`);
- a key file that cannot be read is never replaced by an empty one: OMEMO stays off and the log
  says why. Restore a backup, or move the file away to start with a new identity.

Give each Hermes profile its own file (the default is `<profile home>/xmpp_omemo.json`).

MUC OMEMO is supported when all participants have compatible devices, but group
encryption reliability varies by client. 1:1 encryption is the primary use case.

## Multiple Hermes profiles

XMPP is nice for profile isolation because you can create one JID per profile:

- `sam-hermes@example.org` for your profile
- `mom-hermes@example.org` for Mom's profile
- `dad-hermes@example.org` for Dad's profile

Each Hermes profile gets its own `.env`, memory, sessions, and gateway process.

## Server notes

Prosody needs modules for MUC, HTTP file upload, and the new XEPs. On Debian/Ubuntu:

```bash
sudo apt install prosody
sudo prosodyctl adduser hermes@example.org
```

Enable/configure in Prosody:
- `muc` — group rooms
- `http_file_share` — file upload (XEP-0363)
- `mod_groups` — optional, for ad-hoc command roster

For ejabberd, enable MUC plus `mod_http_upload`. The new features (reactions,
replies, markup) use standard XMPP stanzas and should work on any modern server
supporting XEP-0444, XEP-0461, and XEP-0394.

## Troubleshooting

- `xmpp_auth_failed`: wrong JID/password, or server auth policy issue.
- `xmpp_connect_timeout`: the gateway will report the platform as connected
  right away (connecting doesn't block gateway startup), but a background
  watchdog gives up and retries if the session hasn't actually established
  within `XMPP_CONNECT_TIMEOUT_SECS` (default 180s). A common cause on home
  networks: DNS returns both an A and AAAA record for the server, the IPv6
  route is silently blackholed, and the OS burns through its full TCP retry
  budget (~130s on Linux defaults) before falling back to IPv4 — which then
  connects instantly. Check `python3 -c "import socket;
  print(socket.getaddrinfo('your.server', 5222))"` for a AAAA record, test
  IPv6 reachability separately (`ping6`/`curl -6`), and either fix IPv6
  routing or disable IPv6 for this host if it's not actually usable. If
  raising `XMPP_CONNECT_TIMEOUT_SECS` further fixes it, that confirms this
  cause. Otherwise it's a DNS/firewall/SRV/reachability issue — try
  `XMPP_HOST`/`XMPP_PORT` or check the server is reachable at all.
- `xmpp_connection_lost`: an established session dropped unexpectedly (server
  restart, network blip); the gateway's reconnect watcher should retry.
- `xmpp_lock`: another gateway process is already logged into this JID; stop
  it first, or use a separate bot account per gateway.
- HTTP upload failure: enable XEP-0363 on the server and check max file size.
- DM rejected: add your bare JID to `XMPP_ALLOWED_USERS`.
- MUC silent: add room to `XMPP_MUC_ROOMS` and invite the bot.

## Features in depth

### Message reactions (XEP-0444)

When Hermes starts processing a request, the adapter sends a 👀 reaction. When it
finishes, ✅ or ❌ is sent depending on success or error. Reactions are visible in
clients that support XEP-0444 (Conversations, Dino, Gajim, etc.).

Reactions can also be sent by Hermes tools; the adapter maps the reaction to the
original message using slixmpp's `xep_0444`.

### Threaded replies (XEP-0461)

When you reply to a bot message, the adapter tracks the thread using XEP-0461
message references. Replies from Hermes are sent back into the same thread so
context is preserved.

### Message markup (XEP-0394)

Markdown-like syntax in Hermes responses is converted to XEP-0394 message markup:
- `**bold**` → `<span style="font-weight: bold">bold</span>
- `` `code` `` → `<code>code</code>
- `` ```block``` `` → `<blockcode>block</blockcode>
  
Clients that support XEP-0394 render this natively. Others get the raw text.

### Data forms (XEP-0004) for clarify

When Hermes needs clarification, the adapter sends an XEP-0004 data form instead
of a plain text list. The form includes:
- A hidden `clarify_id` field
- A list-single choice field with the available options
- An "Other (type your own answer)" option for free-form responses

The message body also includes a numbered text fallback so that clients which
do not render `jabber:x:data` forms in `<message>` stanzas (Conversations,
ConverseJS, older Gajim) still display actionable choices. Users on those
clients can reply with the number or option text as normal.

When a client does render the form and the user submits it, the adapter reads
the form response, extracts the `clarify_id` and selected answer, and resolves
the clarification back to Hermes automatically.

**Disabling data forms:** If you prefer to always use plain text clarify
(never send XEP-0004 forms), set the environment variable
`XMPP_DISABLE_CLARIFY_FORMS=true` or add `disable_clarify_forms: true` to your
XMPP platform config in `config.yaml`. This forces the numbered text fallback
path even when the `xep_0004` plugin is registered.

### Voice messages (XEP-0447)

Voice messages from Hermes are sent as XEP-0447 Stateless File Sharing (SFS)
messages. The file is uploaded via XEP-0363 HTTP upload first, then a lightweight
SFS reference is sent. The same upload URL is included in the body and XEP-0066
OOB fallback, with a whole-body XEP-0428 marker. Clients can preview metadata or
use the compatible download reference. The SFS metadata and file hash are kept.

Voice calls (Jingle) are not yet supported because slixmpp does not include a
Jingle RTP implementation.

### Inbound voice notes → speech-to-text

Send exactly one audio attachment without a caption. The plugin accepts OOB,
SFS, and decrypted XEP-0454 links, caches the audio, and passes a `VOICE` event
to Hermes core. Your configured STT provider handles transcription; no provider
endpoint or local audio engine is required by this plugin.

Authorization happens before downloads. MUC requires both a configured room
and a verified personal sender in `muc_allowed_users` (defaults to
`allowed_users`). Room membership alone is no longer sufficient. See
[MUC sender authorization and migration](docs/muc-authorization.md).

Private upload services can use a hostname-scoped exception to Hermes' SSRF
policy. The default trusts the bot's JID domain and subdomains; prefer an exact
upload hostname, or set `"none"` for no private-address exceptions:

```yaml
xmpp:
  voice_trusted_hosts: ["upload.example.org"]
  # voice_transcription: false  # Disable automatic incoming voice processing.
```

The corresponding environment settings are `XMPP_VOICE_TRUSTED_HOSTS` and
`XMPP_VOICE_TRANSCRIPTION`. HTTPS, valid TLS, size limits, and the metadata-address
block remain enforced. Trusted downloads reject redirects and environment
proxies. Encrypted key-bearing URLs are never forwarded as agent text, even
when transcription is disabled or a download fails. See
[voice transport and security details](docs/voice-messages.md).

### Audio calls (Jingle RTP, optional)

Call the bot from Conversations, Dino or any client with audio calls and talk to the agent.
Your speech is transcribed with the configured `stt` provider, the reply is a normal agent
turn in your chat session (tools, memory and approvals apply), and it is spoken with the
configured `tts` provider. This is turn-based speech, not full duplex: expect a few seconds of
delay per turn, mostly the agent's own thinking time.

**Enable it** (off by default). Install the extras into the Hermes environment
(`uv pip install -r requirements-calls.txt`), or, to leave that environment untouched, into a
`vendor/` folder next to `adapter.py` (`uv pip install --python <hermes python> --target vendor -r
requirements-calls.txt`; the plugin adds it to the import path). Then add to `config.yaml`:

```yaml
xmpp:
  calls:
    enabled: true
```

Every option, with its default:

| Option | Default | What it does |
|---|---|---|
| `enabled` (`XMPP_CALLS_ENABLED`) | `false` | Master switch. Off: no call code is loaded. |
| `max_seconds` | `600` | Hang up after this long, whoever is talking. |
| `greeting` | `Hi, I'm here. What can I do for you?` | Spoken when you call the bot. `""` for silence. |
| `silence_ms` | `800` | Pause that ends your turn. Raise it (1000-1500) if you get cut off while thinking; lower it for snappier answers. |
| `vad_level` | `2` | Noise rejection, 0-3. Higher ignores more background noise but may miss soft speech. Steady hum, fans and hiss are ignored at every level. |
| `ack_tone` | `true` | A short beep when your turn is captured, so you know it heard you finish. |
| `idle_timeout` | `180` | Seconds without anyone speaking (and no reply playing or being worked on) before the agent says goodbye and hangs up. `0` turns it off. |
| `log_transcripts` | `false` | Put what was said into the log. Off: the log only has how many words. Call audio is never written to disk. |
| `hangup_grace` | `5` | Seconds you get to speak, after two falling warning beeps, before the agent hangs up (a goodbye, or `hang_up_after`). Speaking cancels the hang-up. `0` hangs up at once. |
| `recap` | `true` | After a call in which you asked for something, the agent texts you one or two sentences saying what you asked and what it did about it (it is silent if you asked for nothing). |
| `allowed_users` | all allowed users except `plaintext_peers` | Who may call, and be called by, the agent. Several people are fine; with more than one the `xmpp_call` tool needs `to`. List explicit JIDs to restrict it. |
| `barge_in` | `queue` | What talking while the agent speaks does. `queue`: nothing is cut off; what you say becomes the next turn (or is steered into the agent's running turn, see below). `stop`: the agent stops talking the moment you speak. |
| `filler_after` | `3.0` | Seconds of waiting for the agent before it says "One moment." |
| `ring_timeout` | `60` | Outgoing calls: ring for up to this many seconds. Phones stop ringing by themselves after about half a minute; the plugin rings again until this time is up (a quick rejection is respected as a refusal). |
| `ring_attempts` | `1` | Outgoing calls: how many times to ring before the message goes out as text. An urgent call can ask for more per call (`attempts`). |
| `ring_pause` | `60` | Seconds between rings when there are several attempts. |
| `busy_wait` | `90` | An outgoing call waits this long for the current call to end instead of failing as busy (a result can arrive just as a call ends). |

Rules: only JIDs in `allowed_users` can call or be called (partner agents, see below, never can); one call at a time (a second caller
hears busy); DTLS-SRTP is required and Opus is the only codec. If the dependencies are not
installed the plugin logs a warning and chat keeps working.

**Beeps.** Three short sounds tell you what the agent heard, without a word:

| Sound | Meaning |
|---|---|
| One short beep | It heard you finish speaking and is sending your turn to the agent. Any noise the voice detector takes for speech also gets one, even if the transcript turns out empty. |
| Two falling beeps | The agent is about to hang up (after a goodbye or with `hang_up_after`). Speak within `hangup_grace` seconds to keep the call open. Only recognised words count: room noise that the voice detector takes for speech does not cancel the hang-up (with `barge_in: queue`). |
| "One moment." (spoken) | The agent is still working after `filler_after` seconds. |

Short transcripts that are only a stock phrase such as "Thank you." or "Thanks for watching" are treated
as noise and dropped, because speech-to-text models often invent them from room sound.

**How it knows you finished.** Speech is told from noise by the WebRTC voice detector. After
`silence_ms` without speech the turn is cut, the beep confirms it, and the agent answers. If you
only paused to think and the agent has not replied yet, the two parts are joined into one turn.
Muting your microphone ends the turn at once. Another person talking in the background still
counts as speech; no frame-level detector can tell them apart from you.

**Talking while the agent talks.** With `barge_in: queue` (default) you may keep talking: the
agent finishes its sentence, then answers everything you said as one message. If the agent is
still working (a tool call, a search) when you speak, your words are sent to it as `/steer`, so
it takes them in after its next tool call without restarting; if that is not possible they are
queued. With `barge_in: stop` your voice cuts the agent off and its next turn is told how much
you heard. Say goodbye and the agent ends the call. A reply that cannot be spoken (TTS down or
busy, after retries) is posted to the chat as text instead.

**Hanging up does not cancel what you asked.** If you hang up in the middle of a request, or the
time limit ends the call, the plugin still transcribes what you were saying, lets the agent finish
(tools included), and sends the answer to your chat as text. You can call back later and carry on.
After every call in which you asked for something, the recap text tells you what the agent
understood and did.

**Missed calls.** If you do not answer an agent's call (after `ring_attempts` rings), the opening is
sent to your chat as text and remembered: when you call back, the agent knows what it wanted to
tell you.

**Sessions.** Every call is its own Hermes session (a DM thread `call-<id>`, titled "Call from|to <person> <date time>", plus the `topic` the agent gave to `xmpp_call`, such as a cron job name), with the full
history in your sessions list. The agent has all the tools of its profile in a call, including
delegation and messaging; it is only asked to keep spoken replies short.

**The agent can call you.** With calls enabled the plugin registers the `xmpp_call` tool
(toolset `xmpp_calls`; enable that toolset for the gateway and for any cron job that should call):

| Argument | What it does |
|---|---|
| `opening` | Required. What the agent says when you pick up: a greeting or a whole briefing in plain spoken sentences. It is synthesized while the phone rings, so there is no gap. |
| `uninterruptible` | `true`: the opening is delivered in full, whatever you say meanwhile (your speech is ignored until it ends). Default `false`. |
| `hang_up_after` | `true`: end the call when the opening is done. Default `false`: the conversation continues and the agent remembers what it said. |
| `to` | Bare JID to call; optional when only one user is callable. |
| `topic` | Short name of what the call is about, such as a cron job name; it ends up in the session title. If the call follows a `CALL REQUEST` message from a partner agent, the plugin takes `topic=` from it when the model leaves it out. |
| `context` | Background the agent keeps for the call and never speaks (a reference and the steps to follow if you ask for a change). Taken from the same `CALL REQUEST` message when omitted. |
| `attempts` | Urgent only: ring this many times (1-5) before the text fallback. Default 1. |
| `pause_seconds` | Seconds between those rings. Default 60. |
| | If the person is already on a call with you, the opening is spoken inside that call instead of ringing again. A call placed while another is running waits up to `busy_wait` seconds for the line. |

Example cron prompt ending: *"When the job is done, call me with xmpp_call: tell me the top
options you considered, why you chose the one you published, and read me today's note.
Use uninterruptible=true."* The tool returns whether the call connected, was declined or
went unanswered, and the call then runs on its own after the job's turn ends.

**Several agents, one operator.** Give each agent its own XMPP account and list the other agents
under `xmpp.agent_peers` (bare JIDs, also `XMPP_AGENT_PEERS`). A partner agent may message the
agent (it must also be in the allowed users) but can never call it or be called. Because two agents
that answer each other never stop, messages from a partner are limited to 4 per 10 minutes, the
gateway's own status lines (tool progress, "interrupting", the silence-marker warning) are never
sent to or acted on from a partner, and the agent is told the sender is a machine.

Hermes has no general `send_message` tool for agents, so the plugin adds `xmpp_message` (toolset
`xmpp_calls`): `to` (an allowed user or a partner agent) and `text`, sent through the running
adapter. It is how a phone agent hands a task to another agent. A typical split: a default agent
runs a cron job and sends the phone agent a `CALL REQUEST` message (briefing, reference, steps for
changes); the phone agent calls you, and if you ask for a change it sends the task back with
`xmpp_message`; the other agent replies `RESULT: ...`, which the phone agent speaks, in the open
call or by calling you again.

**Server requirements (Prosody).** `mod_turn_external` (or `mod_external_services`) with a
reachable coturn so clients and the bot get STUN/TURN credentials (XEP-0215); TLS on c2s. Clients
need Jingle RTP, ICE-UDP and DTLS support (Conversations, Dino and Monal advertise them). The
bot publishes its own call features in its caps, so run it as a normal account.

### Ad-hoc commands (XEP-0050)

The adapter registers ad-hoc commands on the bot's JID. Users can discover them
with their client's command list (e.g., `/cmd` in Conversations). Currently a
basic command list is exposed; future releases may add Hermes-specific commands.

## Development

Use an isolated test environment with `requirements.txt` and
`requirements-dev.txt`; do not modify the managed Hermes environment. Gateway
imports are mocked, while voice tests use real Slixmpp XML and cryptography.

```bash
python -m pytest tests -q -W error -o asyncio_default_fixture_loop_scope=function
```

The optional core-transport tests skip without a Hermes checkout. To include
them, provide its source and transport dependencies in the test environment:

```bash
HERMES_SOURCE=/path/to/hermes-agent python -m pytest tests -q -W error \
  -o asyncio_default_fixture_loop_scope=function
```

Run real `hermes plugins doctor /path/to/plugin --ci` separately. A standalone
suite pass does not establish live gateway or client interoperability.
