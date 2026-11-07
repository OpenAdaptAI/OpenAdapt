import AppKit
import Foundation

struct Row: Decodable { let text: String; let id: String }
let rows = try! JSONDecoder().decode([Row].self, from: CommandLine.arguments[1].data(using: .utf8)!)

final class Source: NSObject, NSTableViewDataSource, NSTableViewDelegate {
    func numberOfRows(in tableView: NSTableView) -> Int { rows.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let field = NSTextField(labelWithString: rows[row].text)
        if !rows[row].id.isEmpty { field.identifier = NSUserInterfaceItemIdentifier(rows[row].id) }
        return field
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let window = NSWindow(contentRect: NSRect(x: 120, y: 120, width: 480, height: 320),
                      styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
window.title = CommandLine.arguments[2]
let table = NSTableView(frame: NSRect(x: 0, y: 0, width: 480, height: 320))
let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("record"))
column.width = 460
table.addTableColumn(column)
let source = Source()
table.dataSource = source
table.delegate = source
let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 480, height: 320))
scroll.documentView = table
window.contentView = scroll
window.makeKeyAndOrderFront(nil)
app.activate(ignoringOtherApps: true)
app.run()
