window.addEventListener("load", function () {
    function getCookie(name) {
        var match = document.cookie.match("(?:^|; )" + name + "=([^;]+)");
        return match ? decodeURIComponent(match[1]) : null;
    }

    // A <form> here would nest inside the change page's own outer <form>
    // (invalid HTML), so this POSTs via fetch() instead, using the CSRF
    // cookie Django's own change-form already set on this same page.
    document.querySelectorAll("[data-ai-audit-confirm]").forEach(function (button) {
        button.addEventListener("click", function () {
            var url = button.getAttribute("data-ai-audit-confirm");
            button.disabled = true;
            fetch(url, {
                method: "POST",
                headers: {"X-CSRFToken": getCookie("csrftoken")},
                credentials: "same-origin",
            }).then(function () {
                window.location.reload();
            }).catch(function () {
                button.disabled = false;
                window.alert("Could not queue the AI audit -- please try again.");
            });
        });
    });
});
