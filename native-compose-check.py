"""Native compose qualification. Run only inside a dedicated Xvfb display."""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time


TITLE = "Markdown Paste qualification fixture"
MARKDOWN = """## GFM heading

[Example link](https://example.invalid/link)

- [x] Task marker
- List marker

| Heading | Cell |
| ------- | ---- |
| Value   | Item |

> Quoted marker

~~~javascript
const testValue = 7; // comment
~~~
"""
RICH_FIXTURES = {
    "chatgpt-shaped": (
        '<div><p>Rich first <a href="https://example.invalid/rich">link</a></p>'
        '<p>Rich second</p><ul><li>List item</li></ul>'
        '<table><tbody><tr><td>Table item</td></tr></tbody></table>'
        '<pre><code>const rich = 1;</code></pre></div>'
    ),
    "browser-shaped": '<article><h2>Rich first</h2><p>Rich second '
    '<a href="https://example.invalid/rich">link</a></p></article>',
    "editor-shaped": '<p><strong>Rich first</strong><br>Rich second '
    '<a href="https://example.invalid/rich">link</a></p>',
}


def retry(operation, description, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = operation()
        if result:
            return result
        time.sleep(0.15)
    raise AssertionError(f"Timed out waiting for {description}")


class Marionette:
    def __init__(self, port):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.stream = self.socket.makefile("rb")
        self.sequence = 0
        greeting = self.receive()
        assert greeting["marionetteProtocol"] >= 3, greeting
        self.call("WebDriver:NewSession", {"capabilities": {}})
        self.call("Marionette:SetContext", {"value": "chrome"})

    def receive(self):
        prefix = bytearray()
        while True:
            character = self.stream.read(1)
            if character == b":":
                break
            if not character:
                raise EOFError("Marionette disconnected")
            prefix.extend(character)
        return json.loads(self.stream.read(int(prefix)))

    def call(self, name, params):
        self.sequence += 1
        data = json.dumps([0, self.sequence, name, params]).encode()
        self.socket.sendall(str(len(data)).encode() + b":" + data)
        kind, request, error, result = self.receive()
        assert (kind, request) == (1, self.sequence)
        if error:
            raise RuntimeError(f"{name}: {error['message']}\n{error.get('stacktrace', '')}")
        return result

    def js(self, script, *args, asynchronous=False):
        name = "WebDriver:ExecuteAsyncScript" if asynchronous else "WebDriver:ExecuteScript"
        return self.call(name, {"script": script, "args": args, "newSandbox": True})["value"]

    def close(self):
        self.stream.close()
        self.socket.close()


def make_profile(root, port):
    local_mail = root / "Mail" / "Local Folders"
    local_mail.mkdir(parents=True)
    (local_mail / "Drafts").touch()
    settings = {
        "marionette.port": port,
        "mail.accountmanager.accounts": "account1",
        "mail.accountmanager.defaultaccount": "account1",
        "mail.accountmanager.localfoldersserver": "server1",
        "mail.account.account1.server": "server1",
        "mail.account.account1.identities": "id1",
        "mail.identity.id1.useremail": "sender@example.invalid",
        "mail.identity.id1.fullName": "Fixture Sender",
        "mail.identity.id1.compose_html": True,
        "mail.identity.id1.draft_folder": "mailbox://nobody@Local%20Folders/Drafts",
        "mail.server.server1.type": "none",
        "mail.server.server1.hostname": "Local Folders",
        "mail.server.server1.userName": "nobody",
        "mail.server.server1.name": "Local Folders",
        "mail.server.server1.directory-rel": "[ProfD]Mail/Local Folders",
        "mail.shell.checkDefaultClient": False,
        "mail.winsearch.enable": False,
        "mailnews.start_page.enabled": False,
        "mailnews.offline_sync_mail": False,
        "extensions.update.enabled": False,
        "toolkit.telemetry.enabled": False,
        "browser.dom.window.dump.enabled": True,
    }
    (root / "user.js").write_text(
        "".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in settings.items()),
        encoding="utf-8",
    )
    return local_mail


def connect(port):
    try:
        return Marionette(port)
    except ConnectionRefusedError:
        return None


COMPOSE_WINDOW = """
const w = Services.wm.getMostRecentWindow("msgcompose");
const editor = w?.document.getElementById("messageEditor");
const d = editor?.contentDocument;
"""


def compose(driver, *, plain=False, signature="", styled=True):
    driver.js(
        """
        const [subject, plain, signature] = arguments;
        const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
        const params = Cc["@mozilla.org/messengercompose/composeparams;1"]
          .createInstance(Ci.nsIMsgComposeParams);
        const fields = Cc["@mozilla.org/messengercompose/composefields;1"]
          .createInstance(Ci.nsIMsgCompFields);
        const identity = MailServices.accounts.getAccount("account1").defaultIdentity;
        identity.htmlSigText = signature === "html" ? "<b>Fixture HTML Signature</b>"
            : signature === "plain" ? "Fixture Plain Signature" : "";
        identity.htmlSigFormat = signature === "html";
        fields.subject = subject;
        fields.to = "recipient@example.invalid";
        params.type = Ci.nsIMsgCompType.New;
        params.format = plain ? Ci.nsIMsgCompFormat.PlainText : Ci.nsIMsgCompFormat.HTML;
        params.composeFields = fields;
        params.identity = identity;
        MailServices.compose.OpenComposeWindowWithParams(null, params);
        """,
        TITLE, plain, signature,
    )
    focus_compose(driver, styled=styled and not plain)


def focus_compose(driver, *, styled=True):
    retry(lambda: driver.js(COMPOSE_WINDOW + """
        return !!(d?.body && w.gMsgCompose?.editor &&
          w.document.getElementById("msgSubject")?.value?.includes(arguments[0]));
        """, TITLE), "the compose editor")
    match = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", TITLE],
        check=True, text=True, capture_output=True, timeout=10,
    ).stdout.splitlines()
    assert len(match) == 1, match
    subprocess.run(["xdotool", "windowfocus", match[0]], check=True, timeout=10)
    driver.js(COMPOSE_WINDOW + """
        editor.focus();
        d.body.focus();
        """)
    retry(lambda: driver.js(COMPOSE_WINDOW + "return d.hasFocus();"), "compose focus")
    if not styled:
        return
    retry(lambda: driver.js(COMPOSE_WINDOW + """
        const fixture = d.createElement("span");
        fixture.className = "markdown-paste-content";
        const link = d.createElement("a");
        link.href = "https://example.invalid/style-probe";
        fixture.append(link);
        d.body.append(fixture);
        const ready = ["rgb(9, 105, 218)", "rgb(88, 166, 255)"]
          .includes(d.defaultView.getComputedStyle(link).color);
        fixture.remove();
        return ready;
        """), "the extension's compose stylesheet")


def close_compose(driver):
    driver.js(COMPOSE_WINDOW + """
        if (w) {
          w.gMsgCompose?.editor?.resetModificationCount();
          w.close();
        }
        """)
    retry(lambda: driver.js(COMPOSE_WINDOW + "return !w;"), "compose window to close")


def key(sequence):
    subprocess.run(["xdotool", "key", "--clearmodifiers", sequence], check=True, timeout=10)


def put_clipboard(driver, plain, rich=None):
    driver.js("""
        const transferable = Cc["@mozilla.org/widget/transferable;1"].createInstance(Ci.nsITransferable);
        transferable.init(null);
        for (const [type, value] of [["text/plain", arguments[0]], ["text/html", arguments[1]]]) {
          if (value === null) continue;
          const data = Cc["@mozilla.org/supports-string;1"].createInstance(Ci.nsISupportsString);
          data.data = value;
          transferable.addDataFlavor(type);
          transferable.setTransferData(type, data);
        }
        Services.clipboard.setData(transferable, null, Ci.nsIClipboard.kGlobalClipboard);
        """, plain, rich)


def state(driver):
    return driver.js(COMPOSE_WINDOW + """
        const body = d.body;
        const signature = body.querySelector(".moz-signature");
        const computed = selector => {
          const node = selector ? body.querySelector(selector) : body;
          if (!node) return null;
          const style = d.defaultView.getComputedStyle(node);
          let effectiveBackground = canvasColor;
          for (let parent = node; parent; parent = parent.parentElement) {
            const candidate = d.defaultView.getComputedStyle(parent).backgroundColor;
            if (/^rgb\(/.test(candidate)) { effectiveBackground = candidate; break; }
          }
          return [style.color, style.backgroundColor, style.marginBlockStart, style.marginBlockEnd, effectiveBackground];
        };
        const canvas = d.createElement("span");
        canvas.style.backgroundColor = "Canvas";
        d.documentElement.append(canvas);
        const canvasColor = d.defaultView.getComputedStyle(canvas).backgroundColor;
        canvas.remove();
        return {
          canvasColor,
          html: body.innerHTML,
          text: body.innerText,
          mode: w.gMsgCompose.composeHTML,
          source: [...body.querySelectorAll("[data-markdown-source]")].map(el => el.dataset.markdownSource),
          elements: Object.fromEntries(["strong", "h2", "table", "pre", "blockquote", "ul", "input[type=checkbox]",
            ".hljs-keyword", ".moz-quote-pre", "img"].map(s => [s, body.querySelectorAll(s).length])),
          links: [...body.querySelectorAll("a")].map(a => a.getAttribute("href")),
          signature: signature?.outerHTML ?? null,
          dark: d.defaultView.matchMedia("(prefers-color-scheme: dark)").matches,
          colors: Object.fromEntries(["", "a", "table td", "blockquote", "pre", ".hljs-keyword",
            ".hljs-comment", ".moz-signature"].map(s => [s, computed(s)])),
          pasted: w.markdownPasteEvents || [],
          selection: d.getSelection().toString(),
        };
        """)


def observe_paste(driver):
    driver.js(COMPOSE_WINDOW + """
        w.markdownPasteEvents = [];
        d.addEventListener("paste", e => w.markdownPasteEvents.push({
          trusted: e.isTrusted, types: [...e.clipboardData.types],
        }), true);
        """)


def paste(driver, plain, rich=None):
    previous = len(state(driver)["pasted"])
    put_clipboard(driver, plain, rich)
    key("ctrl+v")
    snapshot = retry(
        lambda: current if len((current := state(driver))["pasted"]) > previous else None,
        "a native paste event",
    )
    assert snapshot["pasted"][-1]["trusted"], snapshot["pasted"][-1]
    return snapshot


def markdown_editing(driver, signature):
    compose(driver, signature=signature)
    observe_paste(driver)
    original = state(driver)
    if signature:
        assert "Fixture" in (original["signature"] or ""), original
        if signature == "plain":
            assert original["signature"].lower().startswith("<pre"), original
    pasted = paste(driver, MARKDOWN)
    assert pasted["source"] == [MARKDOWN], pasted
    for tag in ("h2", "table", "pre", "blockquote", "ul", "input[type=checkbox]", ".hljs-keyword"):
        assert pasted["elements"][tag], (tag, pasted["html"])
    assert "https://example.invalid/link" in pasted["links"], pasted
    assert pasted["signature"] == original["signature"], (original["signature"], pasted["signature"])
    key("ctrl+z")
    undone = state(driver)
    assert not undone["source"] and "GFM heading" not in undone["text"], undone
    assert undone["signature"] == original["signature"], undone
    key("ctrl+y")
    redone = state(driver)
    assert redone["source"] == [MARKDOWN] and redone["text"].count("GFM heading") == 1, redone
    assert redone["signature"] == original["signature"], redone


def multiple_pastes(driver):
    compose(driver)
    observe_paste(driver)
    paste(driver, "Alpha **bold**")
    two = paste(driver, "Beta **bold**")
    assert "Alpha" in two["text"] and "Beta" in two["text"], two
    key("ctrl+a")
    assert all(word in state(driver)["selection"] for word in ("Alpha", "Beta"))
    replaced = paste(driver, "Replacement **bold**")
    assert replaced["source"] == ["Replacement **bold**"], replaced
    assert "Alpha" not in replaced["text"] and replaced["text"].count("Replacement") == 1, replaced
    key("ctrl+z")
    undone = state(driver)
    assert all(word in undone["text"] for word in ("Alpha", "Beta")), undone


def native_rich(driver, name, *, styled=True):
    compose(driver, styled=styled)
    observe_paste(driver)
    content = paste(driver, "Rich first\nRich second\nlink", RICH_FIXTURES[name])
    assert "text/html" in content["pasted"][-1]["types"], content
    assert not content["source"] and content["links"] == ["https://example.invalid/rich"], content
    assert all(content["text"].count(word) == 1 for word in ("Rich first", "Rich second")), content
    if name == "chatgpt-shaped":
        assert all(content["elements"][tag] for tag in ("table", "ul", "pre")), content
    paragraphs = driver.js(COMPOSE_WINDOW + """
        return [...d.body.querySelectorAll("p")].map(node => ({
          top: node.offsetTop, height: node.offsetHeight,
          margins: [d.defaultView.getComputedStyle(node).marginTop,
                    d.defaultView.getComputedStyle(node).marginBottom],
        }));
        """)
    return {"fixture": name, "html": content["html"], "paragraphs": paragraphs}


def plain_compose(driver):
    compose(driver, plain=True)
    observe_paste(driver)
    snapshot = paste(driver, "Plain **text**")
    assert not snapshot["mode"] and "Plain **text**" in snapshot["text"], snapshot
    assert not snapshot["source"] and not snapshot["elements"]["strong"], snapshot


def toggle(driver):
    compose(driver)
    observe_paste(driver)
    key("T")
    subprocess.run(["xdotool", "type", "oggle **fixture**"], check=True, timeout=10)
    key("ctrl+alt+m")
    rendered = retry(lambda: current if (current := state(driver))["source"] else None, "Markdown toggle")
    assert len(rendered["source"]) == 1, rendered
    assert rendered["source"][0].rstrip("\n") == "Toggle **fixture**", rendered
    key("ctrl+alt+m")
    restored = retry(lambda: current if not (current := state(driver))["source"] else None, "Markdown restoration")
    assert "Toggle **fixture**" in restored["text"], restored


def set_theme(driver, dark):
    theme = driver.js("""
        const dark = arguments[0];
        const done = arguments[arguments.length - 1];
        (async () => {
          const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
          const themes = await AddonManager.getAddonsByTypes(["theme"]);
          const target = themes.find(theme => theme.id.includes(dark ? "compact-dark" : "compact-light"));
          if (!target) return { error: "Built-in theme not found", themes: themes.map(t => t.id) };
          await target.enable();
          return { id: target.id, active: target.isActive };
        })().then(done, error => done({error: String(error)}));
        """, dark, asynchronous=True)
    assert theme.get("active"), theme
    return theme["id"]


def contrast(foreground, background):
    def luminance(color):
        channels = [int(v) / 255 for v in re.findall(r"\d+", color)[:3]]
        return sum(w * (v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
                   for w, v in zip((0.2126, 0.7152, 0.0722), channels))

    first, second = sorted((luminance(foreground), luminance(background)))
    return round((second + 0.05) / (first + 0.05), 2)


def theme_contrast(driver, dark, local_mail):
    theme = set_theme(driver, dark)
    compose(driver, signature="html")
    observe_paste(driver)
    before = state(driver)
    current = paste(driver, MARKDOWN)
    assert current["source"] == [MARKDOWN], current
    assert current["dark"] == dark, (theme, current["dark"], current["colors"])
    colors = current["colors"]
    assert before["signature"] == current["signature"], current
    assert before["colors"][".moz-signature"] == colors[".moz-signature"], current
    assert "background-color:" not in current["html"] and "color:" not in current["html"], current
    assert colors["pre"][1] == ("rgb(22, 27, 34)" if dark else "rgb(246, 248, 250)"), colors
    ratios = {
        selector or "body": contrast(colors[selector][0], colors[selector][4])
        for selector in ("", "a", "table td", "blockquote", ".hljs-keyword", ".hljs-comment")
    }
    if dark:
        assert min(ratios.values()) >= 4.5, (theme, colors, ratios)
    else:
        assert colors["a"][0] == "rgb(9, 105, 218)", colors
    draft = local_draft(driver, local_mail)
    return {"theme": theme, "colors": colors, "estimated_ratios": ratios, "local_draft": draft}


def local_draft(driver, local_mail):
    import mailbox

    drafts = local_mail / "Drafts"
    before = drafts.stat().st_size
    outcome = driver.js(COMPOSE_WINDOW + """
        const done = arguments[arguments.length - 1];
        w.SaveAsDraft().then(() => done("ok"), error => done(String(error)));
        """, asynchronous=True)
    assert outcome == "ok", outcome
    retry(lambda: drafts.stat().st_size > before, "new local draft")
    messages = mailbox.mbox(drafts, create=False)
    try:
        draft = list(messages)[-1]
        decoded = [part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
                   for part in draft.walk() if part.get_content_type() == "text/html"]
        draft_subject = draft["Subject"]
    finally:
        messages.close()
    assert draft_subject == TITLE, draft_subject
    assert len(decoded) == 1 and "GFM heading" in decoded[0], decoded
    assert "markdown-paste-content" in decoded[0], decoded
    assert not re.search(r"#(?:58a6ff|161b22|8b949e|ff7b72)|prefers-color-scheme",
                         decoded[0], re.IGNORECASE), decoded[0]
    return {"subject": draft_subject, "html_part": True, "extension_dark_palette_in_mime": False}


def image_paste(driver, xpi):
    from zipfile import ZipFile

    compose(driver)
    observe_paste(driver)
    with ZipFile(xpi) as archive:
        image = archive.read("icons/icon-48.png")
    subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png"],
                   input=image, stdout=subprocess.DEVNULL, check=True, timeout=10)
    key("ctrl+v")
    snapshot = retry(
        lambda: current if (current := state(driver))["pasted"] else None,
        "native image paste",
    )
    assert snapshot["pasted"][-1]["trusted"] and not snapshot["source"], snapshot
    inserted = retry(
        lambda: current if (current := state(driver))["elements"]["img"] else None,
        "inline image insertion",
    )
    return {"clipboard": snapshot["pasted"][-1]["types"], "inline_images": inserted["elements"]["img"]}


def file_paste(driver, scratch, *, styled=True):
    compose(driver, styled=styled)
    observe_paste(driver)
    fixture = scratch / "fixture.txt"
    fixture.write_text("Local fixture. No contents from a real inbox.", encoding="utf-8")
    driver.js("""
        const path = arguments[0];
        const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
        file.initWithPath(path);
        const transfer = Cc["@mozilla.org/widget/transferable;1"].createInstance(Ci.nsITransferable);
        transfer.init(null);
        transfer.addDataFlavor("application/x-moz-file");
        transfer.setTransferData("application/x-moz-file", file);
        Services.clipboard.setData(transfer, null, Ci.nsIClipboard.kGlobalClipboard);
        """, str(fixture))
    key("ctrl+v")
    snapshot = retry(
        lambda: current if (current := state(driver))["pasted"] else None,
        "native file paste",
    )
    attachments = driver.js(COMPOSE_WINDOW + """
        return [...w.document.getElementById("attachmentBucket").itemChildren]
          .map(item => item.attachment.name);
        """)
    assert snapshot["pasted"][-1]["trusted"] and not snapshot["source"], snapshot
    return {"clipboard": snapshot["pasted"][-1]["types"], "attachments": attachments}


def reply_preserves_quote(driver, scratch):
    fixture = scratch / "quote.eml"
    fixture.write_bytes((
        "From: Fixture Contact <contact@example.invalid>\r\n"
        "To: Fixture Sender <sender@example.invalid>\r\n"
        "Subject: Markdown Paste qualification fixture\r\n"
        "Date: Mon, 23 Nov 2026 12:00:00 +0000\r\n"
        "Message-ID: <quote@example.invalid>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        "<p>Quoted original <b>fixture</b></p>\r\n"
    ).encode())
    result = driver.js("""
        const path = arguments[0];
        const done = arguments[arguments.length - 1];
        const { MailServices } = ChromeUtils.importESModule("resource:///modules/MailServices.sys.mjs");
        const root = MailServices.accounts.localFoldersServer.rootFolder;
        const folder = root.getChildNamed("Drafts");
        const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
        file.initWithPath(path);
        let key;
        MailServices.copy.copyFileMessage(file, folder, null, false, 0, "", {
          QueryInterface: ChromeUtils.generateQI(["nsIMsgCopyServiceListener"]),
          onStartCopy() {},
          onProgress() {},
          setMessageKey(value) { key = value; },
          onStopCopy(status) {
            if (status !== Cr.NS_OK) { done("copy status: " + status); return; }
            try {
              const header = folder.GetMessageHeader(key);
              const uri = folder.getUriForMsg(header);
              const identity = MailServices.accounts.getAccount("account1").defaultIdentity;
              MailServices.compose.OpenComposeWindow("", header, uri, Ci.nsIMsgCompType.Reply,
                                                       Ci.nsIMsgCompFormat.HTML, identity, "", false);
              done("ok");
            } catch (error) { done(String(error)); }
          },
        }, false);
        """, str(fixture), asynchronous=True)
    assert result == "ok", result
    focus_compose(driver)
    observe_paste(driver)
    quote = COMPOSE_WINDOW + 'return d.querySelector("blockquote[type=cite]")?.outerHTML;'
    before = retry(lambda: driver.js(quote), "the original reply quotation")
    assert "Quoted original" in before, before
    driver.js(COMPOSE_WINDOW + "d.getSelection().collapse(d.body, 0);")
    after = paste(driver, "Reply **marker**")
    assert after["source"] == ["Reply **marker**"], after
    assert driver.js(quote) == before, after


def run(binary, xpi, number):
    with tempfile.TemporaryDirectory(prefix="markdown-paste-ci-") as temporary:
        temporary = Path(temporary)
        with socket.socket() as available_socket:
            available_socket.bind(("127.0.0.1", 0))
            port = available_socket.getsockname()[1]
        profile = temporary / "profile"
        local_mail = make_profile(profile, port)
        log = temporary / "client.log"
        app_env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1",
                       MOZ_DISABLE_NONLOCAL_CONNECTIONS="1", MOZ_CRASHREPORTER_DISABLE="1",
                       GDK_BACKEND="x11", GTK_THEME="Adwaita", MOZ_ENABLE_WAYLAND="0")
        with log.open("w") as output:
            process = subprocess.Popen(
                [binary, "-no-remote", "-offline", "-profile", str(profile), "-marionette"],
                stdout=output, stderr=subprocess.STDOUT, env=app_env,
            )
        driver = None
        failed = []
        variant = "PRs #7+#8" if number == 78 else f"PR #{number}"
        client = f"{Path(binary).name} {variant}"
        try:
            driver = retry(lambda: connect(port), "Marionette from the dedicated profile", seconds=40)
            print(client, "version", driver.js("return Services.appinfo.version;"), flush=True)
            addon = driver.js("""
                const [path] = arguments;
                const done = arguments[arguments.length - 1];
                (async () => {
                  const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
                  const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
                  file.initWithPath(path);
                  const addon = await AddonManager.installTemporaryAddon(file);
                  return { id: addon.id, version: addon.version, active: addon.isActive };
                })().then(done, error => done({error: String(error)}));
                """, xpi, asynchronous=True)
            assert addon == {"id": "markdown-paste@andyesys.dev", "version": "1.0.4", "active": True}, addon
            print(client, "ADDON", json.dumps(addon), flush=True)
            cases = (
                [("markdown-" + (signature or "no-signature"), lambda s=signature: markdown_editing(driver, s))
                 for signature in ("", "html", "plain")]
                + [("consecutive-paste-and-selection", lambda: multiple_pastes(driver))]
                + [(name, lambda n=name: native_rich(driver, n)) for name in RICH_FIXTURES]
                + [("plain-compose", lambda: plain_compose(driver)), ("toggle", lambda: toggle(driver)),
                   ("image", lambda: image_paste(driver, xpi)), ("file", lambda: file_paste(driver, temporary)),
                   ("reply-quote", lambda: reply_preserves_quote(driver, temporary))]
                if number == 7 else
                [("dark", lambda: theme_contrast(driver, True, local_mail)),
                 ("light", lambda: theme_contrast(driver, False, local_mail))]
            )
            if number == 78:
                cases = [("combined-undo", lambda: markdown_editing(driver, "plain")),
                         ("combined-dark", lambda: theme_contrast(driver, True, local_mail))]
            results = {}
            for name, test in cases:
                try:
                    result = test()
                    results[name] = result
                    print(client, "PASS", name, json.dumps(result), flush=True)
                except (AssertionError, RuntimeError, subprocess.SubprocessError) as error:
                    failed.append(name)
                    print(client, "FAIL", name, str(error), flush=True)
                finally:
                    close_compose(driver)
            if number == 7:
                disabled = driver.js("""
                    const done = arguments[arguments.length - 1];
                    (async () => {
                      const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
                      const addon = await AddonManager.getAddonByID("markdown-paste@andyesys.dev");
                      await addon.disable();
                      return !addon.isActive;
                    })().then(done, error => done(String(error)));
                    """, asynchronous=True)
                assert disabled is True, disabled
                references = [(name, lambda n=name: native_rich(driver, n, styled=False))
                              for name in RICH_FIXTURES]
                references.append(("file", lambda: file_paste(driver, temporary, styled=False)))
                for name, test in references:
                    try:
                        baseline = test()
                        assert results.get(name) == baseline, (results.get(name), baseline)
                        print(client, "PASS", "native-reference-" + name, json.dumps(baseline), flush=True)
                    except (AssertionError, RuntimeError, subprocess.SubprocessError) as error:
                        failed.append("native-reference-" + name)
                        print(client, "FAIL", "native-reference-" + name, str(error), flush=True)
                    finally:
                        close_compose(driver)
        finally:
            if driver:
                driver.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            print(client, "CLIENT LOG", log.read_text(errors="replace")[-4000:], flush=True)
        return failed


if __name__ == "__main__":
    thunderbird, betterbird, pr7, pr8 = sys.argv[1:]
    failures = []
    from zipfile import ZIP_DEFLATED, ZipFile

    with tempfile.TemporaryDirectory(prefix="markdown-paste-combination-") as folder:
        combined = str(Path(folder) / "combined.xpi")
        with ZipFile(pr7) as first, ZipFile(pr8) as second, ZipFile(combined, "w", ZIP_DEFLATED) as package:
            for entry in first.namelist():
                source = second if entry == "composestyles.css" else first
                package.writestr(entry, source.read(entry))
        for binary in (thunderbird, betterbird):
            for number, xpi in ((7, pr7), (8, pr8), (78, combined)):
                try:
                    failures.extend(run(binary, xpi, number))
                except (AssertionError, RuntimeError, OSError, EOFError) as error:
                    failures.append(f"{Path(binary).name} PR #{number} setup")
                    print("SETUP FAIL", failures[-1], error, flush=True)
    print("Failed cases:", json.dumps(failures), flush=True)
    sys.exit(bool(failures))
