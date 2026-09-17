window.addEventListener("load", function () {
    // UX affordance only -- the server independently re-validates confirm_name on
    // POST regardless (ProjectAdmin.delete_everything_view), so a bypassed/disabled-JS
    // client can't skip the real check.
    var input = document.getElementById("id_confirm_name");
    var button = document.getElementById("delete-everything-submit");
    if (!input || !button) {
        return;
    }
    var expected = input.getAttribute("data-expected-name");
    function refresh() {
        button.disabled = input.value !== expected;
    }
    input.addEventListener("input", refresh);
    refresh();
});
