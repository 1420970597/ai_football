(function () {
    'use strict';
    var active = document.activeElement;
    var dialog = active && active.closest ? active.closest('dialog[open]') : null;
    if (!dialog) {
        var dialogs = document.querySelectorAll('dialog[open]');
        dialog = dialogs.length ? dialogs[dialogs.length - 1] : null;
    }
    if (!dialog) return 'none';
    var request = new Event('cancel', {cancelable: true, bubbles: false});
    if (dialog.dispatchEvent(request) && dialog.open) dialog.close();
    return 'handled';
}())
