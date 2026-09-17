window.addEventListener("load", function () {
    var $ = window.jQuery || (window.django && window.django.jQuery);
    if (!$) {
        return;
    }

    // Shared across every changelist that has list_filter (see each
    // ModelAdmin's own Media) -- Jazzmin's filter bar is a plain GET form,
    // and its own "Search" button is hidden via CSS (stacksense_theme.css)
    // now that this makes it redundant, so this is the ONLY way filters get
    // applied. Submit as soon as ANY filter here changes (some filters also
    // depend on another's current value -- ProjectScopedDepartmentFilter in
    // admin.py, for one -- so a filter picked alone must still refresh the
    // page for those to stay in sync).
    var $filters = $('#changelist-search select.search-filter');
    if (!$filters.length) {
        return;
    }
    $filters.on("change", function () {
        $(this).closest("form").trigger("submit");
    });
});
