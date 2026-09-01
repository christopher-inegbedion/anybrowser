// RelayKit's Safari engine — the native half of the Safari browser backend.
//
// Safari has no CDP: the Web Inspector protocol is gated behind private Apple
// entitlements, and WebDriver only ever drives its own clean-profile Automation
// window, which cannot adopt the user's tabs. What Safari *does* expose is the
// accessibility tree of live web content, and that turns out to be enough for
// the half of the job that JavaScript cannot do: producing input the page
// treats as real. See docs/porting/safari.md for the measurements.
//
// This helper owns the three things the extension cannot do from inside a page:
//
//   * ACTIVATION.  AXPress / AXValue on web content give trusted events that
//     carry user activation, in the background, without moving the cursor.
//   * PIXELS.      ScreenCaptureKit captures a Safari window even when it is
//     occluded or its app is not frontmost.
//   * DIALOGS.     Native JavaScript dialogs are AX objects; pressing their
//     buttons is the equivalent of CDP's Page.handleJavaScriptDialog.
//
// Protocol (newline-delimited JSON, one object per line), mirroring the
// notification helper so both speak the same dialect to the daemon:
//
//   daemon -> engine   {"cmd":"ping","id":"1"}
//                      {"cmd":"windows","id":"2"}
//                      {"cmd":"hit","id":"3","window":"<title substr>","x":120,"y":48}
//                      {"cmd":"press","id":"4","window":"...","x":120,"y":48}
//                      {"cmd":"fill","id":"5","window":"...","x":..,"y":..,"text":"..."}
//                      {"cmd":"dialog","id":"6","action":"accept"|"dismiss"|"peek"}
//                      {"cmd":"screenshot","id":"7","window":"...","crop":true}
//                      {"cmd":"quit"}
//   engine -> daemon   {"id":"3","ok":true,...}   |   {"id":"3","ok":false,"error":"..."}
//                      {"event":"ready","axTrusted":true}
//
// COORDINATES are always *page* coordinates in the target window's web area,
// the same space the planner emits. The engine converts them using the
// AXWebArea's own AXPosition, which is exact and survives sidebars and zoom.
//
// WHY WE WALK THE TREE INSTEAD OF HIT-TESTING: AXUIElementCopyElementAtPosition
// resolves by *screen* point, so when the target window is covered by another
// window it silently returns the wrong window's element — no error, wrong
// answer. Instead we take the target window's AXWebArea frame, add the page
// coordinate to its origin, and search that window's subtree for the deepest
// element whose frame contains the point. Occlusion then cannot matter.

import AppKit
import ApplicationServices
import SafariServices
import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers

// MARK: - line-serialised writer

final class Out {
    private let q = DispatchQueue(label: "relaykit.safari.out")
    func send(_ obj: [String: Any]) {
        q.sync {
            guard let d = try? JSONSerialization.data(withJSONObject: obj, options: []) else { return }
            FileHandle.standardOutput.write(d)
            FileHandle.standardOutput.write("\n".data(using: .utf8)!)
        }
    }
    func ok(_ id: String, _ extra: [String: Any] = [:]) {
        var o: [String: Any] = ["id": id, "ok": true]; extra.forEach { o[$0] = $1 }; send(o)
    }
    func fail(_ id: String, _ msg: String) { send(["id": id, "ok": false, "error": msg]) }
}
let out = Out()

// MARK: - accessibility helpers

func axAttr(_ e: AXUIElement, _ k: String) -> CFTypeRef? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, k as CFString, &v) == .success ? v : nil
}
func axStr(_ e: AXUIElement, _ k: String) -> String? { axAttr(e, k) as? String }
func axFrame(_ e: AXUIElement) -> CGRect? {
    guard let p = axAttr(e, "AXPosition"), let s = axAttr(e, "AXSize") else { return nil }
    var o = CGPoint.zero, z = CGSize.zero
    AXValueGetValue(p as! AXValue, .cgPoint, &o)
    AXValueGetValue(s as! AXValue, .cgSize, &z)
    return CGRect(origin: o, size: z)
}
func axChildren(_ e: AXUIElement) -> [AXUIElement] { axAttr(e, "AXChildren") as? [AXUIElement] ?? [] }

func safariApp() -> (NSRunningApplication, AXUIElement)? {
    guard let app = NSWorkspace.shared.runningApplications
        .first(where: { $0.bundleIdentifier == "com.apple.Safari" }) else { return nil }
    return (app, AXUIElementCreateApplication(app.processIdentifier))
}

func findRole(_ e: AXUIElement, _ role: String, _ depth: Int = 0) -> AXUIElement? {
    if depth > 16 { return nil }
    if axStr(e, "AXRole") == role { return e }
    for c in axChildren(e) { if let r = findRole(c, role, depth + 1) { return r } }
    return nil
}

/// The page a web area is currently showing. `AXURL` is an NSURL, not a
/// String, so an `as? String` cast silently yields nil — which is what made
/// this look unavailable at first.
func webAreaURL(_ web: AXUIElement) -> String? {
    guard let v = axAttr(web, "AXURL") else { return nil }
    if let u = v as? URL { return u.absoluteString }
    return String(describing: v)
}

/// The target window plus its web area.
///
/// `url` identifies the window exactly and is the right way to ask: window
/// titles are page titles, so several windows routinely share one — three
/// windows titled "Example Domain" were open while this was written, at
/// identical screen positions. Matching those by title picks whichever comes
/// first, which means pressing a button on a page that merely looks like the
/// target. `match` (a title substring) remains as a fallback for callers that
/// have no URL, and empty means "the first window with web content".
func webArea(_ appEl: AXUIElement, match: String, url: String = "") -> (win: AXUIElement, web: AXUIElement, title: String)? {
    let windows = (axAttr(appEl, "AXWindows") as? [AXUIElement] ?? [])
    if !url.isEmpty {
        for w in windows {
            guard let web = findRole(w, "AXWebArea") else { continue }
            if webAreaURL(web) == url { return (w, web, axStr(w, "AXTitle") ?? "") }
        }
        return nil            // asked for a specific page; do not settle for another
    }
    for w in windows {
        let t = axStr(w, "AXTitle") ?? ""
        if !match.isEmpty && !t.contains(match) { continue }
        if let web = findRole(w, "AXWebArea") { return (w, web, t) }
    }
    return nil
}

/// The AX tree is not populated the moment the application element is created;
/// the first query after launch can report a window as having no web content.
/// Retry briefly rather than reporting a wrong answer.
func webAreaWaiting(_ appEl: AXUIElement, match: String, url: String = "", tries: Int = 6) -> (win: AXUIElement, web: AXUIElement, title: String)? {
    for i in 0..<tries {
        if let r = webArea(appEl, match: match, url: url) { return r }
        if i < tries - 1 { usleep(120_000) }
    }
    return nil
}

func describe(_ e: AXUIElement) -> [String: Any] {
    var d: [String: Any] = [:]
    d["role"] = axStr(e, "AXRole") ?? ""
    if let s = axStr(e, "AXSubrole") { d["subrole"] = s }
    if let s = axStr(e, "AXTitle"), !s.isEmpty { d["title"] = s }
    if let s = axStr(e, "AXDescription"), !s.isEmpty { d["desc"] = s }
    if let v = axAttr(e, "AXValue") { d["value"] = String(describing: v).prefix(200).description }
    var acts: CFArray?
    AXUIElementCopyActionNames(e, &acts)
    d["actions"] = (acts as? [String]) ?? []
    if let f = axFrame(e) {
        d["frame"] = ["x": f.origin.x, "y": f.origin.y, "w": f.width, "h": f.height]
    }
    return d
}

/// Deepest element in `root`'s subtree whose frame contains `pt` (screen space).
/// Smallest area wins, so a label inside a button resolves to the label.
func deepestAt(_ root: AXUIElement, _ pt: CGPoint) -> AXUIElement? {
    var best: AXUIElement? = nil
    var bestArea = Double.greatestFiniteMagnitude
    func walk(_ e: AXUIElement, _ d: Int) {
        if d > 28 { return }
        if let f = axFrame(e), f.contains(pt) {
            let a = Double(f.width * f.height)
            if a <= bestArea { bestArea = a; best = e }
        }
        for c in axChildren(e) { walk(c, d + 1) }
    }
    walk(root, 0)
    return best
}


/// The deepest element under a point is often an inner span or group that is
/// not the thing you can act on — the placeholder inside a text field, the
/// label inside a button. Walk up to the nearest ancestor that actually
/// supports the operation, and fail loudly if there isn't one.
func actionable(_ el: AXUIElement, for kind: String) -> AXUIElement? {
    var node: AXUIElement? = el
    var depth = 0
    while let n = node, depth < 8 {
        if kind == "press" {
            var acts: CFArray?
            AXUIElementCopyActionNames(n, &acts)
            if ((acts as? [String]) ?? []).contains(kAXPressAction as String) { return n }
        } else {
            var settable = DarwinBoolean(false)
            if AXUIElementIsAttributeSettable(n, "AXValue" as CFString, &settable) == .success,
               settable.boolValue { return n }
        }
        node = axAttr(n, "AXParent") as! AXUIElement?
        depth += 1
    }
    return nil
}

/// Resolve a page coordinate in a named window to an AX element.
/// Convert a viewport point to a screen point and find what is under it.
///
/// `scrollX`/`scrollY` are the page's scroll offset and are **not optional in
/// practice**: an AXWebArea's frame is the *document*, not the visible part, so
/// once a page is scrolled its origin sits above the window (measured:
/// `{y: -771, h: 4600}` at `scrollY = 900`, where the visible top was 129).
/// Treating that origin as the viewport origin subtracts the scroll twice, and
/// every click lands where the page used to be — silently, on whatever element
/// happens to be there. Adding the offset back recovers the visible origin:
/// `-771 + 900 = 129`.
func resolve(match: String, x: Double, y: Double, url: String = "",
             scrollX: Double = 0, scrollY: Double = 0) throws -> (el: AXUIElement, origin: CGPoint, title: String) {
    guard let (_, appEl) = safariApp() else { throw Err("Safari is not running") }
    guard let target = webAreaWaiting(appEl, match: match, url: url) else {
        throw Err(url.isEmpty
                  ? "no Safari window matching \"\(match)\" has web content"
                  : "no Safari window is showing \(url) — is that tab still the one its window displays?")
    }
    guard let wf = axFrame(target.web) else { throw Err("web area has no frame") }
    let pt = CGPoint(x: wf.origin.x + x + scrollX, y: wf.origin.y + y + scrollY)
    guard let el = deepestAt(target.web, pt) else { throw Err("no element at page (\(x), \(y))") }
    return (el, wf.origin, target.title)
}

struct Err: Error, CustomStringConvertible {
    let description: String
    init(_ m: String) { description = m }
}


// MARK: - Safari Settings ▸ Extensions

/// The Extensions pane lists one `AXRow` per extension, each holding a cell
/// with the on/off `AXCheckBox` beside an `AXStaticText` carrying the name.
/// Matching on that text is how a row is identified; there is no identifier.
func extensionRow(_ appEl: AXUIElement, named name: String) -> (row: AXUIElement, toggle: AXUIElement?, label: String)? {
    var found: (AXUIElement, AXUIElement?, String)? = nil
    func walk(_ e: AXUIElement, _ d: Int) {
        if d > 12 || found != nil { return }
        if axStr(e, "AXRole") == "AXRow" {
            var toggle: AXUIElement? = nil
            var label: String? = nil
            func scan(_ n: AXUIElement, _ depth: Int) {
                if depth > 4 { return }
                for c in axChildren(n) {
                    switch axStr(c, "AXRole") ?? "" {
                    case "AXCheckBox": if toggle == nil { toggle = c }
                    case "AXStaticText":
                        if label == nil, let t = axAttr(c, "AXValue") {
                            label = String(describing: t).trimmingCharacters(in: .whitespacesAndNewlines)
                        }
                    default: break
                    }
                    scan(c, depth + 1)
                }
            }
            scan(e, 0)
            // Safari appends the version to the row label on some builds, so a
            // prefix match beats equality here.
            if let l = label, !l.isEmpty, l == name || l.hasPrefix(name) {
                found = (e, toggle, l); return
            }
        }
        for c in axChildren(e) { walk(c, d + 1) }
    }
    for w in (axAttr(appEl, "AXWindows") as? [AXUIElement] ?? []) {
        if (axStr(w, "AXTitle") ?? "") == "Extensions" { walk(w, 0) }
    }
    return found
}

/// A pressable button anywhere in the Extensions window whose title starts with
/// `prefix` — used for "Always Allow on Every Website…", whose exact title
/// carries an ellipsis that differs between builds.
/// A pressable button in the Extensions window matching `test`.
///
/// The ellipsis matters. Granting every site involves two buttons whose titles
/// differ only by it: the pane offers "Always Allow on Every Website**…**",
/// and the confirmation sheet it raises offers "Always Allow on Every Website".
/// A prefix match finds both, so pressing "the button" twice can press the
/// pane's twice — opening the sheet and closing it again, granting nothing
/// while reporting two successful presses.
func extensionButton(_ appEl: AXUIElement, where test: (String) -> Bool) -> AXUIElement? {
    var found: AXUIElement? = nil
    func walk(_ e: AXUIElement, _ d: Int) {
        if d > 14 || found != nil { return }
        if axStr(e, "AXRole") == "AXButton", let t = axStr(e, "AXTitle"), test(t) {
            found = e; return
        }
        for c in axChildren(e) { walk(c, d + 1) }
    }
    // The confirmation sheet is a separate window, so every window is searched.
    for w in (axAttr(appEl, "AXWindows") as? [AXUIElement] ?? []) { walk(w, 0) }
    return found
}

let ALLOW_EVERY_SITE = "Always Allow on Every Website"

func allowEverySiteButton(_ appEl: AXUIElement) -> AXUIElement? {
    extensionButton(appEl) { $0.hasPrefix(ALLOW_EVERY_SITE) && $0 != ALLOW_EVERY_SITE }
}

func allowEverySiteConfirm(_ appEl: AXUIElement) -> AXUIElement? {
    extensionButton(appEl) { $0 == ALLOW_EVERY_SITE }
}

// MARK: - native dialogs

/// A JavaScript dialog is not an AXSheet in Safari — it is an AXGroup holding
/// the message and two buttons. Find it by looking for that button pair.
func findDialog(_ appEl: AXUIElement) -> (group: AXUIElement, message: String, buttons: [String: AXUIElement])? {
    var found: (AXUIElement, String, [String: AXUIElement])? = nil
    func walk(_ e: AXUIElement, _ d: Int) {
        if d > 14 || found != nil { return }
        var buttons: [String: AXUIElement] = [:]
        var texts: [String] = []
        for c in axChildren(e) {
            let role = axStr(c, "AXRole") ?? ""
            if role == "AXButton", let t = axStr(c, "AXTitle"), !t.isEmpty { buttons[t] = c }
            if role == "AXStaticText" || role == "AXTextArea" {
                if let v = axAttr(c, "AXValue") { texts.append(String(describing: v)) }
            }
        }
        if buttons["OK"] != nil || (buttons["Cancel"] != nil && buttons.count >= 2) {
            // Gather text from the whole dialog, not just its direct children.
            // Safari puts the origin banner ("From \u{201C}example.com\u{201D}:") beside the
            // buttons and the actual question a level deeper, so reading only
            // one level reports the banner and drops the thing being asked —
            // which is the part an agent has to decide on.
            var all: [String] = []
            func collect(_ n: AXUIElement, _ depth: Int) {
                if depth > 6 { return }
                for c in axChildren(n) {
                    let r = axStr(c, "AXRole") ?? ""
                    if r == "AXStaticText" || r == "AXTextArea",
                       let v = axAttr(c, "AXValue") {
                        let t = String(describing: v).trimmingCharacters(in: .whitespacesAndNewlines)
                        if !t.isEmpty && !all.contains(t) { all.append(t) }
                    }
                    collect(c, depth + 1)
                }
            }
            collect(e, 0)
            // Safari nests the origin banner beside the buttons and the actual
            // question in a sibling, so a group that yields only one line has
            // almost certainly given us the banner and dropped the question —
            // which is the part an agent has to decide on. Widen to the parent.
            if all.count < 2, let parent = axAttr(e, "AXParent") {
                all.removeAll()
                collect(parent as! AXUIElement, 0)
            }
            let text = (all.isEmpty ? texts : all).joined(separator: " ")
            found = (e, text, buttons); return
        }
        for c in axChildren(e) { walk(c, d + 1) }
    }
    for w in (axAttr(appEl, "AXWindows") as? [AXUIElement] ?? []) { walk(w, 0) }
    return found
}

// MARK: - screenshots

/// Window capture is the *fallback* path: the extension's own captureVisibleTab
/// and the in-page renderer cover the normal cases without needing Screen
/// Recording. ScreenCaptureKit's screenshot API is macOS 14+, so on older
/// systems this one command fails with a clear reason while everything else —
/// activation, dialogs, AppleScript — keeps working.
@available(macOS 14.0, *)
func captureModern(match: String, crop: Bool, reply id: String) {
    guard let (app, appEl) = safariApp() else { return out.fail(id, "Safari is not running") }
    let webOrigin = webArea(appEl, match: match).flatMap { axFrame($0.web) }
    Task {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            let candidates = content.windows.filter {
                $0.owningApplication?.processID == app.processIdentifier && $0.frame.height > 200
                    && (match.isEmpty || ($0.title ?? "").contains(match))
            }
            guard let win = candidates.max(by: { $0.frame.width * $0.frame.height < $1.frame.width * $1.frame.height })
            else { return out.fail(id, "no Safari window matching \"\(match)\"") }
            let cfg = SCStreamConfiguration()
            cfg.width = Int(win.frame.width * 2); cfg.height = Int(win.frame.height * 2)
            cfg.captureResolution = .best
            var img = try await SCScreenshotManager.captureImage(
                contentFilter: SCContentFilter(desktopIndependentWindow: win), configuration: cfg)
            if crop, let wf = webOrigin {
                let r = CGRect(x: (wf.origin.x - win.frame.origin.x) * 2,
                               y: (wf.origin.y - win.frame.origin.y) * 2,
                               width: wf.width * 2, height: wf.height * 2)
                if let c = img.cropping(to: r) { img = c }
            }
            let data = NSMutableData()
            guard let dest = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil)
            else { return out.fail(id, "png encoder unavailable") }
            CGImageDestinationAddImage(dest, img, nil)
            CGImageDestinationFinalize(dest)
            out.ok(id, ["png": (data as Data).base64EncodedString(),
                        "width": img.width, "height": img.height,
                        "cropped": crop && webOrigin != nil])
        } catch {
            out.fail(id, "capture failed: \(error)")
        }
    }
}

func capture(match: String, crop: Bool, reply id: String) {
    if #available(macOS 14.0, *) {
        captureModern(match: match, crop: crop, reply: id)
    } else {
        out.fail(id, "window capture needs macOS 14 or newer; use the extension's own capture")
    }
}

// MARK: - AppleScript (resident, ~5ms vs ~80ms spawning osascript)

final class Osa {
    private var cache: [String: NSAppleScript] = [:]
    private let q = DispatchQueue(label: "relaykit.safari.osa")
    func run(_ src: String) throws -> String {
        try q.sync {
            let script: NSAppleScript
            if let c = cache[src] { script = c }
            else {
                guard let s = NSAppleScript(source: src) else { throw Err("bad AppleScript") }
                cache[src] = s; script = s
            }
            var err: NSDictionary?
            let r = script.executeAndReturnError(&err)
            if let e = err { throw Err(String(describing: e[NSAppleScript.errorMessage] ?? e)) }
            return r.stringValue ?? ""
        }
    }
}
let osa = Osa()

// MARK: - command loop

func handle(_ msg: [String: Any]) {
    let id = (msg["id"] as? String) ?? ""
    let cmd = (msg["cmd"] as? String) ?? ""
    let match = (msg["window"] as? String) ?? ""
    do {
        switch cmd {
        case "ping":
            out.ok(id, ["axTrusted": AXIsProcessTrusted(), "safari": safariApp() != nil])

        // Safari lets an app read whether its own extension is enabled and open
        // Settings at it — and nothing more. There is no enable API anywhere in
        // SafariServices, so setup can report and point, but the switch itself
        // is the user's to flip (or an Accessibility press, which is the same
        // click they would make).
        case "extension-state":
            let identifier = (msg["identifier"] as? String) ?? ""
            guard !identifier.isEmpty else { throw Err("extension-state needs an identifier") }
            let sem = DispatchSemaphore(value: 0)
            var enabled: Bool? = nil
            var failure: String? = nil
            SFSafariExtensionManager.getStateOfSafariExtension(withIdentifier: identifier) { state, error in
                if let error = error as NSError? {
                    // SFErrorNoExtensionFound is the ordinary "not installed yet"
                    // answer during setup, and it is also what Safari returns for
                    // an extension belonging to some other app — the API only
                    // sees extensions bundled inside the calling app.
                    failure = (error.domain == "SFErrorDomain" && error.code == 1)
                        ? "not installed (Safari knows no extension with this identifier)"
                        : error.localizedDescription
                }
                enabled = state?.isEnabled
                sem.signal()
            }
            // Safari answers promptly or not at all; without a bound a missing
            // reply would hang the engine's whole command loop.
            if sem.wait(timeout: .now() + 5) == .timedOut {
                out.ok(id, ["known": false, "detail": "Safari did not answer within 5s"])
            } else if let enabled = enabled {
                out.ok(id, ["known": true, "enabled": enabled])
            } else {
                // No state means Safari has never seen an extension with this id —
                // it is not installed, which is a normal answer, not an error.
                out.ok(id, ["known": false, "detail": failure ?? "no such extension is known to Safari"])
            }

        // Switch an extension on and grant it every site, by pressing the same
        // two controls a person would. Safari offers no API for either — see
        // "extension-state". `dryRun` reports what it found and presses
        // nothing, which is how this is verified without disturbing whatever
        // else the user has installed.
        case "extension-setup":
            let name = (msg["name"] as? String) ?? ""
            guard !name.isEmpty else { throw Err("extension-setup needs a name") }
            let dryRun = (msg["dryRun"] as? Bool) ?? false
            guard let (_, appEl) = safariApp() else { throw Err("Safari is not running") }
            guard let row = extensionRow(appEl, named: name) else {
                throw Err("no extension named \"\(name)\" in Safari Settings — is the Extensions pane open?")
            }
            var report: [String: Any] = ["matched": row.label, "dryRun": dryRun]

            if let toggle = row.toggle {
                let before = (axAttr(toggle, "AXValue") as? Int) ?? 0
                report["enabledBefore"] = before == 1
                if before == 1 {
                    report["enableAction"] = "already on"
                } else if dryRun {
                    report["enableAction"] = "would press"
                } else {
                    let e = AXUIElementPerformAction(toggle, kAXPressAction as CFString)
                    if e != .success { throw Err("could not press the on/off switch (\(e.rawValue))") }
                    usleep(400_000)
                    report["enableAction"] = "pressed"
                    report["enabledAfter"] = ((axAttr(toggle, "AXValue") as? Int) ?? 0) == 1
                }
            } else {
                report["enableAction"] = "no switch found in that row"
            }

            // Selecting the row is what makes the detail pane — and its site
            // access button — appear. Rows expose **no actions at all**, so
            // AXPress is silently useless here; AXSelected is settable and is
            // the way to do it. Done even on a dry run: choosing which
            // extension is displayed changes no setting, and without it the
            // dry run cannot see the site-access state.
            let selErr = AXUIElementSetAttributeValue(row.row, "AXSelected" as CFString, kCFBooleanTrue)
            report["selected"] = selErr == .success
            usleep(500_000)

            // Wait for the detail pane to draw before concluding anything. Read
            // too early — right after the pane opens — and the button is simply
            // not there yet, which this used to report as "already granted".
            // That is the wrong direction to be wrong in: it claims access the
            // extension does not have, and the lie only surfaces later as an
            // agent that cannot see the page.
            // Wait for the *allow* button specifically. "Edit Websites…" is
            // present whether or not access is granted, so treating it as the
            // signal that the pane has drawn exits the loop immediately and
            // reads a pane that is still rendering — which reports "already
            // granted" for an extension that has been granted nothing. Only
            // conclude that after the allow button has had time to appear and
            // has not.
            var allowButton: AXUIElement? = nil
            for attempt in 0..<10 {
                allowButton = allowEverySiteButton(appEl)
                if allowButton != nil { break }
                if attempt < 9 { usleep(200_000) }
            }
            let haveEdit = extensionButton(appEl) { $0.hasPrefix("Edit Websites") } != nil

            if let allow = allowButton {
                if dryRun {
                    report["accessAction"] = "would press"
                } else {
                    let e = AXUIElementPerformAction(allow, kAXPressAction as CFString)
                    report["accessAction"] = (e == .success) ? "pressed" : "press failed (\(e.rawValue))"

                    // Safari raises a confirmation sheet; its button carries the
                    // same words without the ellipsis. Wait for it rather than
                    // assuming, and report what was actually confirmed —
                    // granting nothing while claiming success is the failure
                    // that shows up much later as an agent that sees no page.
                    var confirmed = false
                    for attempt in 0..<12 {
                        if let confirm = allowEverySiteConfirm(appEl) {
                            confirmed = AXUIElementPerformAction(confirm, kAXPressAction as CFString) == .success
                            break
                        }
                        if attempt < 11 { usleep(250_000) }
                    }
                    report["accessConfirm"] = confirmed ? "pressed" : "no confirmation appeared"
                    usleep(600_000)
                    // The truth is whether the grant took, not whether buttons
                    // were pressed.
                    report["accessGranted"] = allowEverySiteButton(appEl) == nil
                }
            } else {
                // Absent once granted: the pane then offers "Edit Websites…".
                report["accessAction"] = haveEdit ? "already granted" : "no site-access control found"
            }
            out.ok(id, report)

        case "extension-prefs":
            let identifier = (msg["identifier"] as? String) ?? ""
            guard !identifier.isEmpty else { throw Err("extension-prefs needs an identifier") }
            let sem = DispatchSemaphore(value: 0)
            var failure: String? = nil
            SFSafariApplication.showPreferencesForExtension(withIdentifier: identifier) { error in
                if let error = error { failure = error.localizedDescription }
                sem.signal()
            }
            _ = sem.wait(timeout: .now() + 5)
            if let failure = failure { throw Err(failure) }
            out.ok(id, ["opened": true])

        case "windows":
            guard let (_, appEl) = safariApp() else { throw Err("Safari is not running") }
            // Same warm-up as resolve(): straight after attaching, the tree can
            // report every window as having no web content. Reporting that as
            // fact is worse than waiting a moment for the truth.
            _ = webAreaWaiting(appEl, match: "")
            var list: [[String: Any]] = []
            for w in (axAttr(appEl, "AXWindows") as? [AXUIElement] ?? []) {
                var d: [String: Any] = ["title": axStr(w, "AXTitle") ?? ""]
                if let f = axFrame(w) { d["frame"] = ["x": f.origin.x, "y": f.origin.y, "w": f.width, "h": f.height] }
                if let web = findRole(w, "AXWebArea"), let wf = axFrame(web) {
                    d["web"] = ["x": wf.origin.x, "y": wf.origin.y, "w": wf.width, "h": wf.height]
                    d["hasWebContent"] = true
                } else { d["hasWebContent"] = false }
                list.append(d)
            }
            out.ok(id, ["windows": list])

        case "hit":
            let r = try resolve(match: match, x: msg["x"] as? Double ?? 0, y: msg["y"] as? Double ?? 0, url: (msg["url"] as? String) ?? "",
                                scrollX: msg["scrollX"] as? Double ?? 0, scrollY: msg["scrollY"] as? Double ?? 0)
            out.ok(id, ["window": r.title, "element": describe(r.el)])

        case "press":
            let r = try resolve(match: match, x: msg["x"] as? Double ?? 0, y: msg["y"] as? Double ?? 0, url: (msg["url"] as? String) ?? "",
                                scrollX: msg["scrollX"] as? Double ?? 0, scrollY: msg["scrollY"] as? Double ?? 0)
            guard let target = actionable(r.el, for: "press") else {
                throw Err("nothing pressable at that point (deepest was \(axStr(r.el, "AXRole") ?? "?"))")
            }
            let e = AXUIElementPerformAction(target, kAXPressAction as CFString)
            if e != .success { throw Err("AXPress failed (\(e.rawValue))") }
            out.ok(id, ["window": r.title, "element": describe(target)])

        case "fill":
            // AXValue on web content is NOT reliably honoured without focus: it
            // takes on an AXComboBox but silently no-ops on a plain AXTextField,
            // returning success either way. So write, read back, and only then
            // escalate to focusing — which raises Safari, so the caller is told.
            let r = try resolve(match: match, x: msg["x"] as? Double ?? 0, y: msg["y"] as? Double ?? 0, url: (msg["url"] as? String) ?? "",
                                scrollX: msg["scrollX"] as? Double ?? 0, scrollY: msg["scrollY"] as? Double ?? 0)
            let text = (msg["text"] as? String) ?? ""
            guard let target = actionable(r.el, for: "fill") else {
                throw Err("nothing fillable at that point (deepest was \(axStr(r.el, "AXRole") ?? "?"))")
            }
            func write() -> Bool {
                _ = AXUIElementSetAttributeValue(target, "AXValue" as CFString, text as CFTypeRef)
                usleep(60_000)
                return (axAttr(target, "AXValue") as? String) == text
            }
            var raised = false
            var verified = write()
            if !verified && (msg["allowRaise"] as? Bool) != false {
                _ = AXUIElementSetAttributeValue(target, "AXFocused" as CFString, kCFBooleanTrue)
                raised = true
                usleep(60_000)
                verified = write()
            }
            if !verified { throw Err("AXValue did not take on \(axStr(target, "AXRole") ?? "?")") }
            out.ok(id, ["window": r.title, "element": describe(target),
                        "raisedSafari": raised, "verified": verified])

        case "dialog":
            guard let (_, appEl) = safariApp() else { throw Err("Safari is not running") }
            guard let d = findDialog(appEl) else { return out.ok(id, ["present": false]) }
            let action = (msg["action"] as? String) ?? "peek"
            if action == "peek" { return out.ok(id, ["present": true, "message": d.message,
                                                     "buttons": Array(d.buttons.keys)]) }
            let wanted = action == "accept" ? ["OK", "Allow", "Yes"] : ["Cancel", "Don't Allow", "No"]
            guard let btn = wanted.compactMap({ d.buttons[$0] }).first
            else { throw Err("no \(action) button; have \(Array(d.buttons.keys))") }
            let e = AXUIElementPerformAction(btn, kAXPressAction as CFString)
            if e != .success { throw Err("dialog AXPress failed (\(e.rawValue))") }
            out.ok(id, ["present": true, "message": d.message, "action": action])

        case "screenshot":
            capture(match: match, crop: (msg["crop"] as? Bool) ?? true, reply: id)

        case "applescript":
            out.ok(id, ["result": try osa.run((msg["source"] as? String) ?? "")])

        case "quit":
            out.ok(id); exit(0)

        default:
            out.fail(id, "unknown command \"\(cmd)\"")
        }
    } catch {
        out.fail(id, String(describing: error))
    }
}

_ = NSApplication.shared
NSApplication.shared.setActivationPolicy(.prohibited)
out.send(["event": "ready", "axTrusted": AXIsProcessTrusted()])

DispatchQueue.global(qos: .userInitiated).async {
    while let line = readLine(strippingNewline: true) {
        guard let d = line.data(using: .utf8),
              let msg = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else {
            out.send(["event": "error", "message": "unparseable line"]); continue
        }
        handle(msg)
    }
    exit(0)   // stdin closed: the daemon is gone
}
RunLoop.main.run()
