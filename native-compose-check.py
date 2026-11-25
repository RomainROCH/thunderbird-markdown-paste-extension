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
const d = w?.GetCurrentEditorElement()?.contentDocument;
"""


def compose(driver, *, plain=False, signature=""):
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
    retry(lambda: driver.js(COMPOSE_WINDOW + """
        return !!(d?.body && w.gMsgCompose?.editor &&
          w.document.getElementById("msgSubject")?.value === arguments[0]);
        """, TITLE), "the compose editor")
    match = subprocess.run(
        ["xdotool", "search", "--onlyvisible", "--name", TITLE],
        check=True, text=True, capture_output=True, timeout=10,
    ).stdout.splitlines()
    assert len(match) == 1, match
    subprocess.run(["xdotool", "windowfocus", match[0]], check=True, timeout=10)
    driver.js(COMPOSE_WINDOW + """
        w.GetCurrentEditorElement().focus();
        d.body.focus();
        """)
    retry(lambda: driver.js(COMPOSE_WINDOW + "return d.hasFocus();"), "compose focus")


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
          return [style.color, style.backgroundColor, style.marginBlockStart, style.marginBlockEnd];
        };
        const canvas = d.createElement("span");
        canvas.style.backgroundColor = "Canvas";
        body.append(canvas);
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


def native_rich(driver, name):
    compose(driver)
    observe_paste(driver)
    content = paste(driver, "Rich first\nRich second\nlink", RICH_FIXTURES[name])
    assert "text/html" in content["pasted"][-1]["types"], content
    assert not content["source"] and content["links"] == ["https://example.invalid/rich"], content
    assert all(content["text"].count(word) == 1 for word in ("Rich first", "Rich second")), content
    if name == "chatgpt-shaped":
        assert all(content["elements"][tag] for tag in ("table", "ul", "pre")), content
    return {"fixture": name, "html": content["html"]}


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
    assert rendered["source"] == ["Toggle **fixture**"], rendered
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


def theme_contrast(driver, dark):
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
    body_back = colors[""][1]
    if body_back == "rgba(0, 0, 0, 0)":
        body_back = current["canvasColor"]
    ratios = {"body": contrast(colors[""][0], body_back)}
    for selector in ("a", "table td", "blockquote", ".hljs-keyword", ".hljs-comment"):
        foreground, *_ = colors[selector]
        back = colors["pre"][1] if dark and selector.startswith(".hljs") else body_back
        ratios[selector] = contrast(foreground, back)
    assert min(ratios.values()) >= 4.5, (theme, colors, ratios)
    return {"theme": theme, "colors": colors, "estimated_ratios": ratios}


def run(binary, xpi, number):
    with tempfile.TemporaryDirectory(prefix="markdown-paste-ci-") as temporary:
        temporary = Path(temporary)
        with socket.socket() as available_socket:
            available_socket.bind(("127.0.0.1", 0))
            port = available_socket.getsockname()[1]
        profile = temporary / "profile"
        make_profile(profile, port)
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
        client = f"{Path(binary).name} PR #{number}"
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
                + [("plain-compose", lambda: plain_compose(driver)), ("toggle", lambda: toggle(driver))]
                if number == 7 else
                [("dark", lambda: theme_contrast(driver, True)), ("light", lambda: theme_contrast(driver, False))]
            )
            for name, test in cases:
                try:
                    result = test()
                    print(client, "PASS", name, json.dumps(result), flush=True)
                except (AssertionError, RuntimeError, subprocess.SubprocessError) as error:
                    failed.append(name)
                    print(client, "FAIL", name, str(error), flush=True)
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
    for binary in (thunderbird, betterbird):
        for number, xpi in ((7, pr7), (8, pr8)):
            try:
                failures.extend(run(binary, xpi, number))
            except (AssertionError, RuntimeError, OSError, EOFError) as error:
                failures.append(f"{Path(binary).name} PR #{number} setup")
                print("SETUP FAIL", failures[-1], error, flush=True)
    print("Failed cases:", json.dumps(failures), flush=True)
    sys.exit(bool(failures))
