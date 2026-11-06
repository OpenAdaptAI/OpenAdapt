// A small native chart app for repair qualification on macOS.
//
// Usage: chart <db.sqlite> <window title> <patient id> <patient banner> <mode>
//
// The window shows a patient banner and a "New note" button. "New note" opens
// a note field and a "Save note" button; "Save note" inserts one row into the
// SQLite table notes(patient_id, note). With mode "update_notice", the first
// "New note" shows an in-window "Update available" notice instead (an overlay
// that covers the window, as many desktop apps draw their notices); "Later"
// dismisses it and opens the note form. Mode "update_sheet" shows the same
// notice as a real AppKit sheet (NSAlert), its own window attached to the
// chart window. Nothing else writes to the database.
import AppKit
import Foundation
import SQLite3

let args = CommandLine.arguments
let dbPath = args[1], title = args[2], patientId = args[3], banner = args[4]
let updateNotice = args.count > 5 && args[5] == "update_notice"
let updateSheet = args.count > 5 && args[5] == "update_sheet"

func insertNote(_ note: String) -> Bool {
    var db: OpaquePointer?
    guard sqlite3_open(dbPath, &db) == SQLITE_OK else { return false }
    defer { sqlite3_close(db) }
    sqlite3_exec(db, "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, patient_id TEXT, note TEXT)", nil, nil, nil)
    var stmt: OpaquePointer?
    guard sqlite3_prepare_v2(db, "INSERT INTO notes (patient_id, note) VALUES (?, ?)", -1, &stmt, nil) == SQLITE_OK else { return false }
    defer { sqlite3_finalize(stmt) }
    let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
    sqlite3_bind_text(stmt, 1, patientId, -1, transient)
    sqlite3_bind_text(stmt, 2, note, -1, transient)
    return sqlite3_step(stmt) == SQLITE_DONE
}

final class Controller: NSObject {
    let window: NSWindow
    let field = NSTextField(frame: NSRect(x: 24, y: 150, width: 420, height: 28))
    let save = NSButton(title: "Save note", target: nil, action: nil)
    let status = NSTextField(labelWithString: "")
    var noticeShown = false

    init(window: NSWindow) {
        self.window = window
        super.init()
        let content = window.contentView!
        let bannerLabel = NSTextField(labelWithString: banner)
        bannerLabel.font = NSFont.systemFont(ofSize: 18, weight: .semibold)
        bannerLabel.frame = NSRect(x: 24, y: 300, width: 500, height: 28)
        content.addSubview(bannerLabel)
        let open = NSButton(title: "New note", target: self, action: #selector(newNote))
        open.frame = NSRect(x: 24, y: 240, width: 140, height: 32)
        content.addSubview(open)
        field.placeholderString = "Note"
        // Centred, so the label a person clicks is the field's structural
        // center too (see benchmark/repair_every_backend, finding 7).
        field.alignment = .center
        field.isHidden = true
        field.setAccessibilityIdentifier("note-field")
        content.addSubview(field)
        save.target = self
        save.action = #selector(saveNote)
        save.frame = NSRect(x: 24, y: 96, width: 140, height: 32)
        save.isHidden = true
        content.addSubview(save)
        status.frame = NSRect(x: 24, y: 40, width: 400, height: 24)
        content.addSubview(status)
    }

    func showForm() {
        field.isHidden = false
        save.isHidden = false
        status.stringValue = "New note for " + banner
    }

    var notice: NSView?

    @objc func newNote() {
        if updateSheet && !noticeShown {
            noticeShown = true
            let alert = NSAlert()
            alert.messageText = "Update available"
            alert.informativeText = "Version 2.1 is ready to install."
            alert.addButton(withTitle: "Later")
            alert.addButton(withTitle: "Install now")
            alert.beginSheetModal(for: window) { response in
                if response == .alertFirstButtonReturn { self.showForm() }
            }
            return
        }
        if updateNotice && !noticeShown {
            noticeShown = true
            let content = window.contentView!
            // A backdrop that blocks the window, and a bordered panel on it.
            let backdrop = NSBox(frame: content.bounds)
            backdrop.boxType = .custom
            backdrop.fillColor = .windowBackgroundColor
            backdrop.borderWidth = 0
            let box = NSBox(frame: NSRect(x: 70, y: 60, width: 420, height: 220))
            box.boxType = .custom
            box.fillColor = .controlBackgroundColor
            box.borderColor = .darkGray
            box.borderWidth = 2
            box.cornerRadius = 0
            box.setAccessibilityRole(.sheet)
            box.setAccessibilityLabel("Update available")
            let heading = NSTextField(labelWithString: "Update available")
            heading.font = NSFont.systemFont(ofSize: 18, weight: .semibold)
            heading.frame = NSRect(x: 30, y: 150, width: 360, height: 28)
            let body = NSTextField(labelWithString: "Version 2.1 is ready to install.")
            body.frame = NSRect(x: 30, y: 110, width: 360, height: 24)
            let later = NSButton(title: "Later", target: self, action: #selector(dismissLater))
            later.frame = NSRect(x: 30, y: 40, width: 120, height: 32)
            let install = NSButton(title: "Install now", target: self, action: #selector(dismissInstall))
            install.frame = NSRect(x: 170, y: 40, width: 140, height: 32)
            for view in [heading, body, later, install] { box.contentView!.addSubview(view) }
            backdrop.contentView!.addSubview(box)
            let notice_root = backdrop
            content.addSubview(notice_root)
            notice = notice_root
            return
        }
        showForm()
    }

    @objc func dismissLater() {
        notice?.removeFromSuperview()
        notice = nil
        showForm()
    }

    @objc func dismissInstall() {
        notice?.removeFromSuperview()
        notice = nil
        status.stringValue = "Installing"
    }

    @objc func saveNote() {
        status.stringValue = insertNote(field.stringValue) ? "Note saved" : "Save failed"
        field.isHidden = true
        save.isHidden = true
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let window = NSWindow(contentRect: NSRect(x: 160, y: 160, width: 560, height: 360),
                      styleMask: [.titled, .closable], backing: .buffered, defer: false)
window.title = title
let controller = Controller(window: window)
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)
app.run()
