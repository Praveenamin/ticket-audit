window.addEventListener("load", function () {
    // #ticket-tid-value only exists on TicketSnapshot's own change page
    // (details_display renders it) -- its presence is what scopes this to
    // just that page, no other check needed.
    var tidEl = document.getElementById("ticket-tid-value");
    var breadcrumb = document.querySelector(".breadcrumb-item.active");
    if (!tidEl || !breadcrumb) {
        return;
    }
    var tid = tidEl.textContent.trim();
    if (!tid) {
        return;
    }

    breadcrumb.textContent = tid;

    function showCopied() {
        var icon = copyBtn.querySelector("i");
        icon.className = "fas fa-check text-success";
        window.setTimeout(function () {
            icon.className = "fas fa-copy";
        }, 1500);
    }

    // navigator.clipboard is undefined outside a secure context (plain HTTP
    // other than localhost) -- real possibility for a local/staging box
    // reached over http://<lan-ip>. window.prompt() was here as a fallback
    // but reads as "a modal popped up," not "copied" -- this copies via the
    // older execCommand API instead, silently, so the confirmation is the
    // same either way.
    function legacyCopy(text) {
        var textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        try {
            document.execCommand("copy");
        } catch (err) {
            // best effort -- nothing else to fall back to here
        }
        document.body.removeChild(textarea);
    }

    var copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "btn btn-link btn-sm p-0 ms-2";
    copyBtn.title = "Copy ticket ID";
    copyBtn.innerHTML = '<i class="fas fa-copy"></i>';
    copyBtn.addEventListener("click", function (event) {
        event.preventDefault();
        if (navigator.clipboard) {
            navigator.clipboard.writeText(tid).then(showCopied);
        } else {
            legacyCopy(tid);
            showCopied();
        }
    });
    breadcrumb.appendChild(copyBtn);
});
