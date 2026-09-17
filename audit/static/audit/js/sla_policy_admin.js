window.addEventListener("load", function () {
    var $ = window.jQuery || (window.django && window.django.jQuery);
    if (!$ || !$("#id_project").length || !$("#id_department").length) {
        return;
    }

    var $department = $("#id_department");
    var $allOptions = $department.find("option").clone();

    function syncDepartmentOptions() {
        var projectId = $("#id_project").val();
        var currentValue = $department.val();

        $department.empty();
        $allOptions.each(function () {
            var $option = $(this);
            var optionProject = $option.attr("data-project");
            if (!optionProject || optionProject === projectId) {
                $department.append($option.clone());
            }
        });

        if ($department.find("option[value='" + currentValue + "']").length) {
            $department.val(currentValue);
        } else {
            $department.val("");
        }
    }

    $("#id_project").on("change", syncDepartmentOptions);
    syncDepartmentOptions();
});
