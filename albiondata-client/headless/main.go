// Command-line build of the Albion Data Client: packet capture and upload only.
//
// The upstream entry point (../albiondata-client.go) also starts a Wails
// dashboard window, tray icon and self-updater, which need WebKitGTK on
// Linux. This entry point runs the same client without any of that, so it
// only depends on libpcap. Log output goes to the terminal.
package main

import (
	"os"

	"github.com/ao-data/albiondata-client/client"
	"github.com/ao-data/albiondata-client/log"
)

var version string

func init() {
	client.ConfigGlobal.SetupFlags()
}

func main() {
	if client.ConfigGlobal.PrintVersion {
		log.Infof("Albion Data Client (headless), version: %s", version)
		return
	}

	log.Infof("Albion Data Client (headless) %s starting. Press Ctrl+C to stop.", version)
	if err := client.NewClient(version).Run(); err != nil {
		log.Error(err)
		log.Error("Packet capture has stopped due to an error.")
		os.Exit(1)
	}
}
