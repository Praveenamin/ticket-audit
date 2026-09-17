window.addEventListener("load", function () {
    var $ = window.jQuery || (window.django && window.django.jQuery);
    if (!$ || !$("#id_source_type").length) {
        return;
    }

    function syncApiFieldVisibility() {
        var showApiFields = $("#id_source_type").val() === "api";
        // S3 archival only ever runs from the API sync path (sync.py) -- exactly as
        // meaningless on a dump-sourced project as the WHMCS API fields themselves,
        // so it hides/shows on the same condition.
        $(".field-whmcs_base_url, .field-whmcs_api_identifier, .field-whmcs_api_secret, "
            + ".field-s3_archive_enabled, .field-s3_bucket_name, .field-s3_access_key_id, "
            + ".field-s3_secret_access_key, .field-s3_region")
            .toggle(showApiFields);
        // "Upload new dump" only makes sense for a dump-sourced project --
        // showing it unconditionally (as before) reads as if it's asking
        // for a dump upload even while switching a project to API mode.
        $(".field-upload_dump_link").toggle(!showApiFields);
        // Mirror image: testing API connectivity only makes sense once
        // there's an API endpoint/credentials to test.
        $(".field-test_connectivity_link").toggle(showApiFields);
    }

    $("#id_source_type").on("change", syncApiFieldVisibility);
    syncApiFieldVisibility();
});
