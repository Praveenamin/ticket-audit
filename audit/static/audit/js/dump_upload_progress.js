window.addEventListener("load", function () {
    // .dump-upload-in-progress (from _dump_upload_status_badge) only renders
    // while a DumpUpload is queued/processing -- if it's on the page at all,
    // something is actively being imported, so keep reloading until a fresh
    // load no longer finds it (i.e. it reached done/failed).
    if (document.querySelector(".dump-upload-in-progress")) {
        window.setInterval(function () {
            window.location.reload();
        }, 8000);
    }
});
