window.addEventListener("load", function () {
    var $ = window.jQuery || (window.django && window.django.jQuery);
    if (!$ || !$("#id_source_type").length) {
        return;
    }

    function syncApiFieldVisibility() {
        var showApiFields = $("#id_source_type").val() === "api";
        $(".field-whmcs_base_url, .field-whmcs_api_identifier, .field-whmcs_api_secret")
            .toggle(showApiFields);
    }

    $("#id_source_type").on("change", syncApiFieldVisibility);
    syncApiFieldVisibility();
});
