import Carbon.HIToolbox
import Foundation

// Carbon RegisterEventHotKey needs no Accessibility permission and consumes the
// key system-wide. Bare backslash (kVK_ANSI_Backslash, no modifiers) is the
// chosen talk key; the menu-bar toggle releases it when he needs to type '\'.
private let pikePillSignature: OSType = 0x5050494C // 'PPIL'

private let pikePillHotkeyHandler: EventHandlerUPP = { _, eventRef, userData in
    guard let eventRef, let userData else { return noErr }
    var hotKeyID = EventHotKeyID()
    let status = GetEventParameter(
        eventRef,
        EventParamName(kEventParamDirectObject),
        EventParamType(typeEventHotKeyID),
        nil,
        MemoryLayout<EventHotKeyID>.size,
        nil,
        &hotKeyID
    )
    guard status == noErr, hotKeyID.signature == pikePillSignature else { return status }
    let manager = Unmanaged<HotkeyManager>.fromOpaque(userData).takeUnretainedValue()
    manager.firePressed()
    return noErr
}

final class HotkeyManager {
    private var handlerRef: EventHandlerRef?
    private var hotkeyRef: EventHotKeyRef?
    private var onPressed: (() -> Void)?

    var isRegistered: Bool { hotkeyRef != nil }

    func firePressed() {
        onPressed?()
    }

    func register(onPressed: @escaping () -> Void) throws {
        unregister()
        self.onPressed = onPressed

        var eventSpec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: OSType(kEventHotKeyPressed)
        )
        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            pikePillHotkeyHandler,
            1,
            &eventSpec,
            Unmanaged.passUnretained(self).toOpaque(),
            &handlerRef
        )
        guard installStatus == noErr else {
            throw NSError(domain: "HermesVoice", code: Int(installStatus), userInfo: [NSLocalizedDescriptionKey: "hotkey handler install failed: \(installStatus)"])
        }

        let hotKeyID = EventHotKeyID(signature: pikePillSignature, id: 1)
        let registerStatus = RegisterEventHotKey(
            UInt32(kVK_ANSI_Backslash),
            0, // no modifiers: bare backslash
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotkeyRef
        )
        guard registerStatus == noErr else {
            throw NSError(domain: "HermesVoice", code: Int(registerStatus), userInfo: [NSLocalizedDescriptionKey: "backslash hotkey register failed: \(registerStatus)"])
        }
    }

    func unregister() {
        if let hotkeyRef {
            UnregisterEventHotKey(hotkeyRef)
            self.hotkeyRef = nil
        }
        if let handlerRef {
            RemoveEventHandler(handlerRef)
            self.handlerRef = nil
        }
    }

    deinit {
        unregister()
    }
}
