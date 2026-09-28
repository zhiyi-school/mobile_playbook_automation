/** Placeholder SwiftUI app built so Xcode issues a provisioning profile for re-signing repackaged IPAs. */

import SwiftUI

@main
struct RepackProvisionApp: App {
    /** A single window showing the provisioning stub label. */
    var body: some Scene {
        WindowGroup { Text("Repackaging test provisioning stub") }
    }
}
