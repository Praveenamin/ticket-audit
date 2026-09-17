window.addEventListener("load", function () {
    // The user-menu dropdown trigger is one of several .nav-link buttons in
    // the top-right nav group (theme switcher, language switcher, this one)
    // -- its fa-user icon is the one detail unique to it, so target that
    // rather than guessing position. Its title attribute already carries
    // the username (rendered server-side for the tooltip); this just makes
    // it visible instead of hidden in a hover-only title.
    var icon = document.querySelector(".navbar-nav.ms-auto .fa-user");
    var trigger = icon && icon.closest("a.nav-link");
    if (!trigger || trigger.querySelector(".navbar-username")) {
        return;
    }
    var name = trigger.getAttribute("title");
    if (!name) {
        return;
    }
    var span = document.createElement("span");
    span.className = "navbar-username";
    span.textContent = name;
    trigger.appendChild(span);
});
