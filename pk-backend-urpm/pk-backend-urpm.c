/*
 * PackageKit backend for urpm-ng
 *
 * This backend communicates with the urpm D-Bus service (org.mageia.Urpm.v1)
 * to provide package management functionality to GNOME Software and KDE Discover.
 *
 * Copyright (C) 2026 Mageia Community
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "pk-backend.h"
#include <packagekit-glib2/packagekit.h>
#include <gio/gio.h>
#include <json-glib/json-glib.h>
#include <string.h>

#define URPM_BUS_NAME      "org.mageia.Urpm.v1"
#define URPM_OBJECT_PATH   "/org/mageia/Urpm/v1"
#define URPM_INTERFACE     "org.mageia.Urpm.v1"

typedef struct {
    GDBusConnection *connection;
    GDBusProxy *proxy;
    guint progress_signal_id;
    guint complete_signal_id;
} PkBackendUrpmPrivate;

static PkBackendUrpmPrivate *priv = NULL;

/* ========================================================================= */
/* D-Bus connection management                                               */
/* ========================================================================= */

static gboolean
ensure_connection(PkBackendJob *job, GError **error)
{
    if (priv->connection != NULL && !g_dbus_connection_is_closed(priv->connection))
        return TRUE;

    /* Clear old connection */
    g_clear_object(&priv->proxy);
    g_clear_object(&priv->connection);

    priv->connection = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, error);
    if (priv->connection == NULL)
        return FALSE;

    priv->proxy = g_dbus_proxy_new_sync(
        priv->connection,
        G_DBUS_PROXY_FLAGS_NONE,
        NULL,
        URPM_BUS_NAME,
        URPM_OBJECT_PATH,
        URPM_INTERFACE,
        NULL,
        error
    );

    return priv->proxy != NULL;
}

/* ========================================================================= */
/* Helper: Parse JSON package list                                           */
/* ========================================================================= */

static void
emit_packages_from_json(PkBackendJob *job, const gchar *json_str, PkInfoEnum info)
{
    JsonParser *parser = json_parser_new();
    GError *error = NULL;

    if (!json_parser_load_from_data(parser, json_str, -1, &error)) {
        g_warning("Failed to parse JSON: %s", error->message);
        g_error_free(error);
        g_object_unref(parser);
        return;
    }

    JsonNode *root = json_parser_get_root(parser);
    if (!JSON_NODE_HOLDS_ARRAY(root)) {
        g_object_unref(parser);
        return;
    }

    JsonArray *packages = json_node_get_array(root);
    guint len = json_array_get_length(packages);

    for (guint i = 0; i < len; i++) {
        JsonObject *pkg = json_array_get_object_element(packages, i);
        if (pkg == NULL)
            continue;

        const gchar *name = json_object_get_string_member_with_default(pkg, "name", "");
        const gchar *version = json_object_get_string_member_with_default(pkg, "version", "");
        const gchar *release = json_object_get_string_member_with_default(pkg, "release", "");
        const gchar *arch = json_object_get_string_member_with_default(pkg, "arch", "");
        const gchar *summary = json_object_get_string_member_with_default(pkg, "summary", "");
        gboolean installed = json_object_get_boolean_member_with_default(pkg, "installed", FALSE);

        /* Build package_id: name;version-release;arch;urpm */
        g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
        g_autofree gchar *package_id = pk_package_id_build(name, evr, arch, "urpm");

        /* Override info enum based on installed status */
        PkInfoEnum pkg_info = info;
        if (info == PK_INFO_ENUM_AVAILABLE && installed)
            pkg_info = PK_INFO_ENUM_INSTALLED;

        pk_backend_job_package(job, pkg_info, package_id, summary);
    }

    g_object_unref(parser);
}

/* ========================================================================= */
/* Backend entry points                                                      */
/* ========================================================================= */

const gchar *
pk_backend_get_description(PkBackend *backend)
{
    return "urpm-ng backend for Mageia Linux";
}

const gchar *
pk_backend_get_author(PkBackend *backend)
{
    return "Mageia Community <mageia-dev@mageia.org>";
}

void
pk_backend_initialize(GKeyFile *conf, PkBackend *backend)
{
    priv = g_new0(PkBackendUrpmPrivate, 1);
}

void
pk_backend_destroy(PkBackend *backend)
{
    if (priv != NULL) {
        g_clear_object(&priv->proxy);
        g_clear_object(&priv->connection);
        g_free(priv);
        priv = NULL;
    }
}

PkBitfield
pk_backend_get_groups(PkBackend *backend)
{
    return pk_bitfield_from_enums(
        PK_GROUP_ENUM_ACCESSIBILITY,
        PK_GROUP_ENUM_ADMIN_TOOLS,
        PK_GROUP_ENUM_COMMUNICATION,
        PK_GROUP_ENUM_DESKTOP_GNOME,
        PK_GROUP_ENUM_DESKTOP_KDE,
        PK_GROUP_ENUM_DESKTOP_OTHER,
        PK_GROUP_ENUM_EDUCATION,
        PK_GROUP_ENUM_FONTS,
        PK_GROUP_ENUM_GAMES,
        PK_GROUP_ENUM_GRAPHICS,
        PK_GROUP_ENUM_INTERNET,
        PK_GROUP_ENUM_MULTIMEDIA,
        PK_GROUP_ENUM_NETWORK,
        PK_GROUP_ENUM_OFFICE,
        PK_GROUP_ENUM_OTHER,
        PK_GROUP_ENUM_PROGRAMMING,
        PK_GROUP_ENUM_PUBLISHING,
        PK_GROUP_ENUM_SECURITY,
        PK_GROUP_ENUM_SYSTEM,
        PK_GROUP_ENUM_VIRTUALIZATION,
        -1
    );
}

PkBitfield
pk_backend_get_filters(PkBackend *backend)
{
    return pk_bitfield_from_enums(
        PK_FILTER_ENUM_INSTALLED,
        PK_FILTER_ENUM_NOT_INSTALLED,
        PK_FILTER_ENUM_ARCH,
        PK_FILTER_ENUM_NEWEST,
        -1
    );
}

gchar **
pk_backend_get_mime_types(PkBackend *backend)
{
    const gchar *mime_types[] = {
        "application/x-rpm",
        NULL
    };
    return g_strdupv((gchar **)mime_types);
}

gboolean
pk_backend_supports_parallelization(PkBackend *backend)
{
    return FALSE;
}

/* ========================================================================= */
/* Search                                                                    */
/* ========================================================================= */

static void
pk_backend_search_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield filters;
    gchar **values;
    GError *error = NULL;

    g_variant_get(params, "(t^a&s)", &filters, &values);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    /* Join search terms */
    g_autofree gchar *pattern = g_strjoinv(" ", values);

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "SearchPackages",
        g_variant_new("(sb)", pattern, FALSE),  /* pattern, search_provides */
        G_DBUS_CALL_FLAGS_NONE,
        -1,
        NULL,
        &error
    );

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "Search failed: %s", error->message);
        g_error_free(error);
        return;
    }

    const gchar *json_str;
    g_variant_get(result, "(&s)", &json_str);

    /* Determine info based on filter */
    PkInfoEnum info = PK_INFO_ENUM_AVAILABLE;
    if (pk_bitfield_contain(filters, PK_FILTER_ENUM_INSTALLED))
        info = PK_INFO_ENUM_INSTALLED;

    emit_packages_from_json(job, json_str, info);

    g_variant_unref(result);
    pk_backend_job_finished(job);
}

void
pk_backend_search_names(PkBackend *backend, PkBackendJob *job,
                        PkBitfield filters, gchar **values)
{
    pk_backend_job_thread_create(job, pk_backend_search_thread, NULL, NULL);
}

void
pk_backend_search_details(PkBackend *backend, PkBackendJob *job,
                          PkBitfield filters, gchar **values)
{
    /* Same as search_names for now */
    pk_backend_job_thread_create(job, pk_backend_search_thread, NULL, NULL);
}

/* ========================================================================= */
/* Get Updates                                                               */
/* ========================================================================= */

static void
pk_backend_get_updates_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    GError *error = NULL;

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "GetUpdates",
        NULL,
        G_DBUS_CALL_FLAGS_NONE,
        -1,
        NULL,
        &error
    );

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "GetUpdates failed: %s", error->message);
        g_error_free(error);
        return;
    }

    const gchar *json_str;
    g_variant_get(result, "(&s)", &json_str);

    /* Parse updates JSON */
    JsonParser *parser = json_parser_new();
    if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
        JsonNode *root = json_parser_get_root(parser);
        if (JSON_NODE_HOLDS_OBJECT(root)) {
            JsonObject *obj = json_node_get_object(root);
            JsonArray *upgrades = json_object_get_array_member(obj, "upgrades");

            if (upgrades != NULL) {
                guint len = json_array_get_length(upgrades);
                for (guint i = 0; i < len; i++) {
                    JsonObject *pkg = json_array_get_object_element(upgrades, i);
                    if (pkg == NULL)
                        continue;

                    const gchar *name = json_object_get_string_member_with_default(pkg, "name", "");
                    const gchar *nevra = json_object_get_string_member_with_default(pkg, "nevra", "");
                    const gchar *arch = json_object_get_string_member_with_default(pkg, "arch", "");

                    /* Extract version from nevra (name-version-release.arch) */
                    g_autofree gchar *evr = NULL;
                    const gchar *dash1 = strrchr(nevra, '-');
                    if (dash1) {
                        const gchar *dash2 = g_strrstr_len(nevra, dash1 - nevra, "-");
                        if (dash2) {
                            evr = g_strdup(dash2 + 1);
                            /* Remove .arch suffix */
                            gchar *dot = strrchr(evr, '.');
                            if (dot) *dot = '\0';
                        }
                    }
                    if (evr == NULL)
                        evr = g_strdup("0");

                    g_autofree gchar *package_id = pk_package_id_build(name, evr, arch, "urpm");
                    pk_backend_job_package(job, PK_INFO_ENUM_NORMAL, package_id, "");
                }
            }
        }
    }
    g_object_unref(parser);

    g_variant_unref(result);
    pk_backend_job_finished(job);
}

void
pk_backend_get_updates(PkBackend *backend, PkBackendJob *job, PkBitfield filters)
{
    pk_backend_job_thread_create(job, pk_backend_get_updates_thread, NULL, NULL);
}

/* ========================================================================= */
/* Refresh Cache                                                             */
/* ========================================================================= */

static void
pk_backend_refresh_cache_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    GError *error = NULL;

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_REFRESH_CACHE);
    pk_backend_job_set_percentage(job, PK_BACKEND_PERCENTAGE_INVALID);

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "RefreshMetadata",
        NULL,
        G_DBUS_CALL_FLAGS_NONE,
        300000,  /* 5 min timeout */
        NULL,
        &error
    );

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "Refresh failed: %s", error->message);
        g_error_free(error);
        return;
    }

    gboolean success;
    const gchar *message;
    g_variant_get(result, "(b&s)", &success, &message);

    if (!success) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "Refresh failed: %s", message);
    }

    pk_backend_job_set_percentage(job, 100);
    g_variant_unref(result);
    pk_backend_job_finished(job);
}

void
pk_backend_refresh_cache(PkBackend *backend, PkBackendJob *job, gboolean force)
{
    pk_backend_job_thread_create(job, pk_backend_refresh_cache_thread, NULL, NULL);
}

/* ========================================================================= */
/* Install Packages                                                          */
/* ========================================================================= */

/* Context for async install with progress */
typedef struct {
    PkBackendJob *job;
    GMainLoop *loop;
    GVariant *result;
    GError *error;
    gchar **package_ids;
    guint signal_id;
    gboolean in_download;
} InstallContext;

static void
on_operation_progress(GDBusConnection *connection,
                      const gchar *sender_name,
                      const gchar *object_path,
                      const gchar *interface_name,
                      const gchar *signal_name,
                      GVariant *parameters,
                      gpointer user_data)
{
    InstallContext *ctx = user_data;
    const gchar *op_id, *phase, *package, *message;
    guint32 current, total;

    /* Python signal format: (sssuus) = (op_id, phase, package, current, total, message) */
    g_variant_get(parameters, "(&s&s&suu&s)",
                  &op_id, &phase, &package, &current, &total, &message);

    /* Calculate percentage based on phase */
    guint percentage = 0;
    if (total > 0) {
        percentage = (current * 100) / total;
    }

    /* Update status based on phase */
    if (g_str_equal(phase, "downloading")) {
        if (!ctx->in_download) {
            ctx->in_download = TRUE;
            pk_backend_job_set_status(ctx->job, PK_STATUS_ENUM_DOWNLOAD);
        }
        /* Download is 0-50% of total progress */
        pk_backend_job_set_percentage(ctx->job, percentage / 2);
    } else if (g_str_equal(phase, "installing")) {
        if (ctx->in_download) {
            ctx->in_download = FALSE;
            pk_backend_job_set_status(ctx->job, PK_STATUS_ENUM_INSTALL);
        }
        /* Install is 50-100% of total progress */
        pk_backend_job_set_percentage(ctx->job, 50 + percentage / 2);
    } else if (g_str_equal(phase, "resolving")) {
        pk_backend_job_set_status(ctx->job, PK_STATUS_ENUM_DEP_RESOLVE);
        pk_backend_job_set_percentage(ctx->job, 0);
    }
}

static void
install_packages_ready_cb(GObject *source, GAsyncResult *res, gpointer user_data)
{
    InstallContext *ctx = user_data;
    ctx->result = g_dbus_proxy_call_finish(G_DBUS_PROXY(source), res, &ctx->error);
    g_main_loop_quit(ctx->loop);
}

static void
pk_backend_install_packages_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield flags;
    gchar **package_ids;
    GError *error = NULL;

    g_variant_get(params, "(t^a&s)", &flags, &package_ids);

    gboolean simulate = pk_bitfield_contain(flags, PK_TRANSACTION_FLAG_ENUM_SIMULATE);

    g_message("pk_backend_install_packages_thread: starting (simulate=%d)", simulate);

    if (!ensure_connection(job, &error)) {
        g_warning("pk_backend_install_packages_thread: connection failed: %s", error->message);
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_DEP_RESOLVE);
    pk_backend_job_set_percentage(job, 0);

    /* Extract package names from package_ids */
    GPtrArray *names = g_ptr_array_new();
    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts != NULL && parts[0] != NULL) {
            g_debug("pk_backend_install_packages_thread: package %s", parts[0]);
            g_ptr_array_add(names, g_strdup(parts[0]));
        }
    }
    g_ptr_array_add(names, NULL);

    GVariantBuilder builder;
    g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));
    for (guint i = 0; i < names->len - 1; i++) {
        g_variant_builder_add(&builder, "s", g_ptr_array_index(names, i));
    }
    GVariant *pkg_array = g_variant_builder_end(&builder);

    if (simulate) {
        /* SIMULATE mode: just preview, no download/install */
        g_message("pk_backend_install_packages_thread: calling PreviewInstall (simulate)");

        GVariant *result = g_dbus_proxy_call_sync(
            priv->proxy,
            "PreviewInstall",
            g_variant_new("(@as)", pkg_array),
            G_DBUS_CALL_FLAGS_NONE,
            60000,
            NULL,
            &error
        );

        g_ptr_array_free(names, TRUE);

        if (result == NULL) {
            g_warning("PreviewInstall failed: %s", error->message);
            pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                      "Preview failed: %s", error->message);
            g_error_free(error);
            pk_backend_job_finished(job);
            return;
        }

        /* Don't emit packages during simulation - let Discover query via resolve */
        g_variant_unref(result);
        g_variant_unref(result);
        pk_backend_job_set_percentage(job, 100);
        pk_backend_job_finished(job);
        return;
    }

    /* REAL mode: do the actual install */
    g_message("pk_backend_install_packages_thread: calling InstallPackages with %d packages", names->len - 1);

    /* Set up context for async operation */
    InstallContext ctx = {
        .job = job,
        .loop = g_main_loop_new(NULL, FALSE),
        .result = NULL,
        .error = NULL,
        .package_ids = package_ids,
        .signal_id = 0,
        .in_download = FALSE
    };

    /* Subscribe to progress signals */
    ctx.signal_id = g_dbus_connection_signal_subscribe(
        priv->connection,
        URPM_BUS_NAME,
        URPM_INTERFACE,
        "OperationProgress",
        URPM_OBJECT_PATH,
        NULL,
        G_DBUS_SIGNAL_FLAGS_NONE,
        on_operation_progress,
        &ctx,
        NULL
    );

    /* Make async call */
    g_dbus_proxy_call(
        priv->proxy,
        "InstallPackages",
        g_variant_new("(@as@a{sv})",
                      pkg_array,
                      g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0)),
        G_DBUS_CALL_FLAGS_NONE,
        600000,  /* 10 min timeout */
        NULL,
        install_packages_ready_cb,
        &ctx
    );

    /* Run main loop until call completes */
    g_main_loop_run(ctx.loop);

    /* Unsubscribe from signals */
    g_dbus_connection_signal_unsubscribe(priv->connection, ctx.signal_id);
    g_main_loop_unref(ctx.loop);
    g_ptr_array_free(names, TRUE);

    if (ctx.result == NULL) {
        g_warning("pk_backend_install_packages_thread: D-Bus call failed: %s", ctx.error->message);
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "Install failed: %s", ctx.error->message);
        g_error_free(ctx.error);
        return;
    }

    gboolean success;
    const gchar *message;
    g_variant_get(ctx.result, "(b&s)", &success, &message);

    g_message("pk_backend_install_packages_thread: result success=%d message=%s", success, message);

    if (!success) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_PACKAGE_FAILED_TO_INSTALL,
                                  "Install failed: %s", message);
    } else {
        /* Parse the JSON response and emit packages as FINISHED */
        GError *json_error = NULL;
        JsonParser *parser = json_parser_new();
        if (json_parser_load_from_data(parser, message, -1, &json_error)) {
            JsonNode *root = json_parser_get_root(parser);
            if (JSON_NODE_HOLDS_OBJECT(root)) {
                JsonObject *obj = json_node_get_object(root);
                JsonArray *packages = json_object_get_array_member(obj, "packages");

                if (packages != NULL) {
                    guint len = json_array_get_length(packages);
                    for (guint j = 0; j < len; j++) {
                        JsonObject *pkg = json_array_get_object_element(packages, j);
                        if (pkg == NULL)
                            continue;

                        const gchar *name = json_object_get_string_member_with_default(pkg, "name", "");
                        const gchar *version = json_object_get_string_member_with_default(pkg, "version", "");
                        const gchar *release = json_object_get_string_member_with_default(pkg, "release", "");
                        const gchar *arch = json_object_get_string_member_with_default(pkg, "arch", "");

                        g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
                        g_autofree gchar *pkg_id = pk_package_id_build(name, evr, arch, "urpm");
                        pk_backend_job_package(job, PK_INFO_ENUM_FINISHED, pkg_id, "");
                    }
                }
            }
        } else {
            if (json_error) g_error_free(json_error);
        }
        g_object_unref(parser);
    }

    pk_backend_job_set_percentage(job, 100);
    g_variant_unref(ctx.result);
    pk_backend_job_finished(job);
}

void
pk_backend_install_packages(PkBackend *backend, PkBackendJob *job,
                            PkBitfield transaction_flags, gchar **package_ids)
{
    pk_backend_job_thread_create(job, pk_backend_install_packages_thread, NULL, NULL);
}

/* ========================================================================= */
/* Remove Packages                                                           */
/* ========================================================================= */

static void
pk_backend_remove_packages_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield flags;
    gchar **package_ids;
    gboolean allow_deps, autoremove;
    GError *error = NULL;

    g_variant_get(params, "(t^a&sbb)", &flags, &package_ids, &allow_deps, &autoremove);

    gboolean simulate = pk_bitfield_contain(flags, PK_TRANSACTION_FLAG_ENUM_SIMULATE);

    g_message("pk_backend_remove_packages_thread: starting (simulate=%d)", simulate);

    if (simulate) {
        /* SIMULATE mode: just emit the packages that would be removed */
        pk_backend_job_set_status(job, PK_STATUS_ENUM_DEP_RESOLVE);
        for (guint i = 0; package_ids[i] != NULL; i++) {
            pk_backend_job_package(job, PK_INFO_ENUM_REMOVING, package_ids[i], "");
        }
        pk_backend_job_set_percentage(job, 100);
        pk_backend_job_finished(job);
        return;
    }

    /* REAL mode: do the actual removal */
    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_REMOVE);
    pk_backend_job_set_percentage(job, PK_BACKEND_PERCENTAGE_INVALID);

    /* Extract package names from package_ids */
    GPtrArray *names = g_ptr_array_new();
    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts != NULL && parts[0] != NULL)
            g_ptr_array_add(names, g_strdup(parts[0]));
    }
    g_ptr_array_add(names, NULL);

    GVariantBuilder builder;
    g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));
    for (guint i = 0; i < names->len - 1; i++) {
        g_variant_builder_add(&builder, "s", g_ptr_array_index(names, i));
    }
    GVariant *pkg_array = g_variant_builder_end(&builder);

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "RemovePackages",
        g_variant_new("(@as@a{sv})",
                      pkg_array,
                      g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0)),
        G_DBUS_CALL_FLAGS_NONE,
        300000,  /* 5 min timeout */
        NULL,
        &error
    );

    g_ptr_array_free(names, TRUE);

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "Remove failed: %s", error->message);
        g_error_free(error);
        return;
    }

    gboolean success;
    const gchar *message;
    g_variant_get(result, "(b&s)", &success, &message);

    if (!success) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_PACKAGE_FAILED_TO_REMOVE,
                                  "Remove failed: %s", message);
    } else {
        /* Emit the removed packages */
        for (guint i = 0; package_ids[i] != NULL; i++) {
            pk_backend_job_package(job, PK_INFO_ENUM_REMOVING, package_ids[i], "");
        }
    }

    pk_backend_job_set_percentage(job, 100);
    g_variant_unref(result);
    pk_backend_job_finished(job);
}

void
pk_backend_remove_packages(PkBackend *backend, PkBackendJob *job,
                           PkBitfield transaction_flags, gchar **package_ids,
                           gboolean allow_deps, gboolean autoremove)
{
    pk_backend_job_thread_create(job, pk_backend_remove_packages_thread, NULL, NULL);
}

/* ========================================================================= */
/* Update Packages (Upgrade)                                                 */
/* ========================================================================= */

static void
pk_backend_update_packages_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield flags;
    gchar **package_ids;
    GError *error = NULL;

    g_variant_get(params, "(t^a&s)", &flags, &package_ids);

    gboolean simulate = pk_bitfield_contain(flags, PK_TRANSACTION_FLAG_ENUM_SIMULATE);

    g_message("pk_backend_update_packages_thread: starting (simulate=%d)", simulate);

    if (simulate) {
        /* SIMULATE mode: just emit the packages that would be updated */
        pk_backend_job_set_status(job, PK_STATUS_ENUM_DEP_RESOLVE);
        for (guint i = 0; package_ids[i] != NULL; i++) {
            pk_backend_job_package(job, PK_INFO_ENUM_UPDATING, package_ids[i], "");
        }
        pk_backend_job_set_percentage(job, 100);
        pk_backend_job_finished(job);
        return;
    }

    /* REAL mode: do the actual upgrade */
    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_UPDATE);
    pk_backend_job_set_percentage(job, PK_BACKEND_PERCENTAGE_INVALID);

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "UpgradePackages",
        g_variant_new("(@a{sv})",
                      g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0)),
        G_DBUS_CALL_FLAGS_NONE,
        1800000,  /* 30 min timeout */
        NULL,
        &error
    );

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_INTERNAL_ERROR,
                                  "Upgrade failed: %s", error->message);
        g_error_free(error);
        return;
    }

    gboolean success;
    const gchar *message;
    g_variant_get(result, "(b&s)", &success, &message);

    if (!success) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_PACKAGE_FAILED_TO_INSTALL,
                                  "Upgrade failed: %s", message);
    }

    pk_backend_job_set_percentage(job, 100);
    g_variant_unref(result);
    pk_backend_job_finished(job);
}

void
pk_backend_update_packages(PkBackend *backend, PkBackendJob *job,
                           PkBitfield transaction_flags, gchar **package_ids)
{
    pk_backend_job_thread_create(job, pk_backend_update_packages_thread, NULL, NULL);
}

/* ========================================================================= */
/* Get Package Details                                                       */
/* ========================================================================= */

static void
pk_backend_get_details_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    gchar **package_ids;
    GError *error = NULL;

    g_variant_get(params, "(^a&s)", &package_ids);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts == NULL || parts[0] == NULL)
            continue;

        GVariant *result = g_dbus_proxy_call_sync(
            priv->proxy,
            "GetPackageInfo",
            g_variant_new("(s)", parts[0]),
            G_DBUS_CALL_FLAGS_NONE,
            -1,
            NULL,
            &error
        );

        if (result == NULL) {
            g_warning("GetPackageInfo failed: %s", error->message);
            g_clear_error(&error);
            continue;
        }

        const gchar *json_str;
        g_variant_get(result, "(&s)", &json_str);

        JsonParser *parser = json_parser_new();
        if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
            JsonNode *root = json_parser_get_root(parser);
            if (JSON_NODE_HOLDS_OBJECT(root)) {
                JsonObject *pkg = json_node_get_object(root);

                const gchar *description = json_object_get_string_member_with_default(
                    pkg, "description", "");
                const gchar *url = json_object_get_string_member_with_default(
                    pkg, "url", "");
                const gchar *license = json_object_get_string_member_with_default(
                    pkg, "license", "");
                gint64 size = json_object_get_int_member_with_default(
                    pkg, "size", 0);

                pk_backend_job_details(job, package_ids[i],
                                       NULL,  /* summary (already have from package) */
                                       license,
                                       PK_GROUP_ENUM_OTHER,
                                       description,
                                       url,
                                       (gulong)size,
                                       0);  /* download_size */
            }
        }
        g_object_unref(parser);
        g_variant_unref(result);
    }

    pk_backend_job_finished(job);
}

void
pk_backend_get_details(PkBackend *backend, PkBackendJob *job, gchar **package_ids)
{
    pk_backend_job_thread_create(job, pk_backend_get_details_thread, NULL, NULL);
}

/* ========================================================================= */
/* Resolve (name to package_id)                                              */
/* ========================================================================= */

static void
pk_backend_resolve_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield filters;
    gchar **packages;
    GError *error = NULL;

    g_variant_get(params, "(t^a&s)", &filters, &packages);

    /* Count packages for logging */
    guint pkg_count = 0;
    for (guint i = 0; packages[i] != NULL; i++) pkg_count++;
    g_debug("pk_backend_resolve_thread: filters=0x%lx, %u packages", (unsigned long)filters, pkg_count);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    /* Build array of package names for batch resolve */
    GVariantBuilder builder;
    g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));

    for (guint i = 0; packages[i] != NULL; i++) {
        /* Extract package name - input might be a package_id or just a name */
        g_auto(GStrv) parts = pk_package_id_split(packages[i]);
        const gchar *name = (parts != NULL) ? parts[PK_PACKAGE_ID_NAME] : packages[i];
        g_variant_builder_add(&builder, "s", name);
    }

    /* Single D-Bus call for all packages */
    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "ResolvePackages",
        g_variant_new("(@as)", g_variant_builder_end(&builder)),
        G_DBUS_CALL_FLAGS_NONE,
        300000,  /* 5 min timeout for large batches */
        NULL,
        &error
    );

    if (result == NULL) {
        g_warning("ResolvePackages failed: %s", error->message);
        g_error_free(error);
        pk_backend_job_finished(job);
        return;
    }

    const gchar *json_str;
    g_variant_get(result, "(&s)", &json_str);

    /* Parse results */
    JsonParser *parser = json_parser_new();
    if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
        JsonNode *root = json_parser_get_root(parser);
        if (JSON_NODE_HOLDS_ARRAY(root)) {
            JsonArray *arr = json_node_get_array(root);
            guint len = json_array_get_length(arr);

            for (guint i = 0; i < len; i++) {
                JsonObject *pkg = json_array_get_object_element(arr, i);
                if (pkg == NULL)
                    continue;

                /* Skip packages not found */
                if (json_object_has_member(pkg, "found") &&
                    !json_object_get_boolean_member(pkg, "found"))
                    continue;

                const gchar *name = json_object_get_string_member_with_default(
                    pkg, "name", "");
                const gchar *version = json_object_get_string_member_with_default(
                    pkg, "version", "");
                const gchar *release = json_object_get_string_member_with_default(
                    pkg, "release", "");
                const gchar *arch = json_object_get_string_member_with_default(
                    pkg, "arch", "");
                const gchar *summary = json_object_get_string_member_with_default(
                    pkg, "summary", "");
                gboolean installed = json_object_get_boolean_member_with_default(
                    pkg, "installed", FALSE);

                /* Apply filters */
                if (pk_bitfield_contain(filters, PK_FILTER_ENUM_INSTALLED) && !installed)
                    continue;
                if (pk_bitfield_contain(filters, PK_FILTER_ENUM_NOT_INSTALLED) && installed)
                    continue;

                /* Skip if missing version info */
                if (version[0] == '\0' || arch[0] == '\0')
                    continue;

                g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
                g_autofree gchar *package_id = pk_package_id_build(
                    name, evr, arch, "urpm");

                PkInfoEnum info = installed ? PK_INFO_ENUM_INSTALLED
                                            : PK_INFO_ENUM_AVAILABLE;
                pk_backend_job_package(job, info, package_id, summary);
            }
        }
    }
    g_object_unref(parser);
    g_variant_unref(result);

    pk_backend_job_finished(job);
}

void
pk_backend_resolve(PkBackend *backend, PkBackendJob *job,
                   PkBitfield filters, gchar **packages)
{
    pk_backend_job_thread_create(job, pk_backend_resolve_thread, NULL, NULL);
}

/* ========================================================================= */
/* Cancel                                                                    */
/* ========================================================================= */

void
pk_backend_cancel(PkBackend *backend, PkBackendJob *job)
{
    /* Signal cancellation - the D-Bus service should handle this */
    GError *error = NULL;

    if (priv->proxy == NULL) {
        pk_backend_job_finished(job);
        return;
    }

    /* Try to call CancelOperation on D-Bus service */
    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "CancelOperation",
        NULL,
        G_DBUS_CALL_FLAGS_NONE,
        5000,
        NULL,
        &error
    );

    if (result != NULL) {
        g_variant_unref(result);
    } else {
        /* Cancellation is best-effort */
        g_clear_error(&error);
    }

    pk_backend_job_finished(job);
}

/* ========================================================================= */
/* Get Update Detail                                                         */
/* ========================================================================= */

void
pk_backend_get_update_detail(PkBackend *backend, PkBackendJob *job,
                             gchar **package_ids)
{
    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    /* Return minimal update detail for each package */
    for (guint i = 0; package_ids[i] != NULL; i++) {
        pk_backend_job_update_detail(job, package_ids[i],
                                     NULL,  /* updates */
                                     NULL,  /* obsoletes */
                                     NULL,  /* vendor_urls */
                                     NULL,  /* bugzilla_urls */
                                     NULL,  /* cve_urls */
                                     PK_RESTART_ENUM_NONE,
                                     "Update available",  /* update_text */
                                     NULL,  /* changelog */
                                     PK_UPDATE_STATE_ENUM_STABLE,
                                     NULL,  /* issued */
                                     NULL); /* updated */
    }

    pk_backend_job_finished(job);
}

/* ========================================================================= */
/* Stubs for required but not-yet-implemented functions                      */
/* ========================================================================= */

static void
pk_backend_get_packages_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield filters;
    GError *error = NULL;

    g_variant_get(params, "(t)", &filters);

    /* Only support INSTALLED filter for now */
    if (!pk_bitfield_contain(filters, PK_FILTER_ENUM_INSTALLED)) {
        /* For NOT_INSTALLED or no filter, we'd need to return all available packages
           which could be huge. Just finish for now. */
        pk_backend_job_finished(job);
        return;
    }

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "GetInstalledPackages",
        NULL,
        G_DBUS_CALL_FLAGS_NONE,
        120000,  /* 2 min timeout */
        NULL,
        &error
    );

    if (result == NULL) {
        g_warning("GetInstalledPackages failed: %s", error->message);
        g_error_free(error);
        pk_backend_job_finished(job);
        return;
    }

    const gchar *json_str;
    g_variant_get(result, "(&s)", &json_str);

    JsonParser *parser = json_parser_new();
    if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
        JsonNode *root = json_parser_get_root(parser);
        if (JSON_NODE_HOLDS_ARRAY(root)) {
            JsonArray *arr = json_node_get_array(root);
            guint len = json_array_get_length(arr);

            for (guint i = 0; i < len; i++) {
                JsonObject *pkg = json_array_get_object_element(arr, i);
                if (pkg == NULL)
                    continue;

                const gchar *name = json_object_get_string_member_with_default(pkg, "name", "");
                const gchar *version = json_object_get_string_member_with_default(pkg, "version", "");
                const gchar *release = json_object_get_string_member_with_default(pkg, "release", "");
                const gchar *arch = json_object_get_string_member_with_default(pkg, "arch", "");
                const gchar *summary = json_object_get_string_member_with_default(pkg, "summary", "");

                if (name[0] == '\0' || version[0] == '\0')
                    continue;

                g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
                g_autofree gchar *package_id = pk_package_id_build(name, evr, arch, "urpm");

                pk_backend_job_package(job, PK_INFO_ENUM_INSTALLED, package_id, summary);
            }
        }
    }
    g_object_unref(parser);
    g_variant_unref(result);

    pk_backend_job_finished(job);
}

void
pk_backend_get_packages(PkBackend *backend, PkBackendJob *job, PkBitfield filters)
{
    pk_backend_job_thread_create(job, pk_backend_get_packages_thread, NULL, NULL);
}

static void
pk_backend_depends_on_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield filters;
    gchar **package_ids;
    gboolean recursive;
    GError *error = NULL;

    g_variant_get(params, "(t^a&sb)", &filters, &package_ids, &recursive);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts == NULL || parts[0] == NULL)
            continue;

        /* Call PreviewInstall to get dependencies */
        GVariantBuilder builder;
        g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));
        g_variant_builder_add(&builder, "s", parts[0]);
        GVariant *pkg_array = g_variant_builder_end(&builder);

        GVariant *result = g_dbus_proxy_call_sync(
            priv->proxy,
            "PreviewInstall",
            g_variant_new("(@as)", pkg_array),
            G_DBUS_CALL_FLAGS_NONE,
            60000,
            NULL,
            &error
        );

        if (result == NULL) {
            g_warning("PreviewInstall failed: %s", error->message);
            g_clear_error(&error);
            continue;
        }

        const gchar *json_str;
        g_variant_get(result, "(&s)", &json_str);

        /* Parse dependencies from preview result */
        JsonParser *parser = json_parser_new();
        if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
            JsonNode *root = json_parser_get_root(parser);
            if (JSON_NODE_HOLDS_OBJECT(root)) {
                JsonObject *obj = json_node_get_object(root);
                JsonArray *to_install = json_object_get_array_member(obj, "to_install");

                if (to_install != NULL) {
                    guint len = json_array_get_length(to_install);
                    for (guint j = 0; j < len; j++) {
                        JsonObject *pkg = json_array_get_object_element(to_install, j);
                        if (pkg == NULL)
                            continue;

                        const gchar *name = json_object_get_string_member_with_default(pkg, "name", "");
                        const gchar *version = json_object_get_string_member_with_default(pkg, "version", "");
                        const gchar *release = json_object_get_string_member_with_default(pkg, "release", "");
                        const gchar *arch = json_object_get_string_member_with_default(pkg, "arch", "");
                        const gchar *summary = json_object_get_string_member_with_default(pkg, "summary", "");

                        /* Skip the package itself */
                        if (g_strcmp0(name, parts[0]) == 0)
                            continue;

                        g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
                        g_autofree gchar *dep_id = pk_package_id_build(name, evr, arch, "urpm");
                        pk_backend_job_package(job, PK_INFO_ENUM_AVAILABLE, dep_id, summary);
                    }
                }
            }
        }
        g_object_unref(parser);
        g_variant_unref(result);
    }

    pk_backend_job_finished(job);
}

void
pk_backend_depends_on(PkBackend *backend, PkBackendJob *job, PkBitfield filters,
                       gchar **package_ids, gboolean recursive)
{
    pk_backend_job_thread_create(job, pk_backend_depends_on_thread, NULL, NULL);
}

static void
pk_backend_required_by_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield filters;
    gchar **package_ids;
    gboolean recursive;
    GError *error = NULL;

    g_variant_get(params, "(t^a&sb)", &filters, &package_ids, &recursive);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts == NULL)
            continue;

        GVariant *result = g_dbus_proxy_call_sync(
            priv->proxy,
            "WhatRequires",
            g_variant_new("(s)", parts[PK_PACKAGE_ID_NAME]),
            G_DBUS_CALL_FLAGS_NONE,
            30000,
            NULL,
            &error
        );

        if (result == NULL) {
            g_warning("WhatRequires failed: %s", error->message);
            g_clear_error(&error);
            continue;
        }

        const gchar *json_str;
        g_variant_get(result, "(&s)", &json_str);

        JsonParser *parser = json_parser_new();
        if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
            JsonNode *root = json_parser_get_root(parser);
            if (JSON_NODE_HOLDS_ARRAY(root)) {
                JsonArray *arr = json_node_get_array(root);
                guint len = json_array_get_length(arr);

                for (guint j = 0; j < len; j++) {
                    JsonObject *pkg = json_array_get_object_element(arr, j);
                    if (pkg == NULL)
                        continue;

                    const gchar *name = json_object_get_string_member_with_default(pkg, "name", "");
                    const gchar *version = json_object_get_string_member_with_default(pkg, "version", "");
                    const gchar *release = json_object_get_string_member_with_default(pkg, "release", "");
                    const gchar *arch = json_object_get_string_member_with_default(pkg, "arch", "");
                    const gchar *summary = json_object_get_string_member_with_default(pkg, "summary", "");

                    if (name[0] == '\0')
                        continue;

                    g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
                    g_autofree gchar *package_id = pk_package_id_build(name, evr, arch, "urpm");

                    pk_backend_job_package(job, PK_INFO_ENUM_AVAILABLE, package_id, summary);
                }
            }
        }
        g_object_unref(parser);
        g_variant_unref(result);
    }

    pk_backend_job_finished(job);
}

void
pk_backend_required_by(PkBackend *backend, PkBackendJob *job, PkBitfield filters,
                        gchar **package_ids, gboolean recursive)
{
    pk_backend_job_thread_create(job, pk_backend_required_by_thread, NULL, NULL);
}

static void
pk_backend_get_files_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    gchar **package_ids;
    GError *error = NULL;

    g_variant_get(params, "(^a&s)", &package_ids);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts == NULL)
            continue;

        /* Build NEVRA from package_id parts: name-version-release.arch */
        /* parts[0]=name, parts[1]=evr (version-release), parts[2]=arch */
        g_autofree gchar *nevra = g_strdup_printf("%s-%s.%s",
            parts[PK_PACKAGE_ID_NAME],
            parts[PK_PACKAGE_ID_VERSION],
            parts[PK_PACKAGE_ID_ARCH]);

        GVariant *result = g_dbus_proxy_call_sync(
            priv->proxy,
            "GetPackageFiles",
            g_variant_new("(s)", nevra),
            G_DBUS_CALL_FLAGS_NONE,
            30000,
            NULL,
            &error
        );

        if (result == NULL) {
            g_warning("GetPackageFiles failed: %s", error->message);
            g_clear_error(&error);
            continue;
        }

        const gchar *json_str;
        g_variant_get(result, "(&s)", &json_str);

        /* Parse JSON array of file paths */
        JsonParser *parser = json_parser_new();
        if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
            JsonNode *root = json_parser_get_root(parser);
            if (JSON_NODE_HOLDS_ARRAY(root)) {
                JsonArray *arr = json_node_get_array(root);
                guint len = json_array_get_length(arr);

                /* Build file list */
                g_autoptr(GPtrArray) file_array = g_ptr_array_new_with_free_func(g_free);
                for (guint j = 0; j < len; j++) {
                    const gchar *file_path = json_array_get_string_element(arr, j);
                    if (file_path)
                        g_ptr_array_add(file_array, g_strdup(file_path));
                }
                g_ptr_array_add(file_array, NULL);

                pk_backend_job_files(job, package_ids[i], (gchar **)file_array->pdata);
            }
        }
        g_object_unref(parser);
        g_variant_unref(result);
    }

    pk_backend_job_finished(job);
}

void
pk_backend_get_files(PkBackend *backend, PkBackendJob *job, gchar **package_ids)
{
    pk_backend_job_thread_create(job, pk_backend_get_files_thread, NULL, NULL);
}

static void
pk_backend_download_packages_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    gchar **package_ids;
    const gchar *directory;
    GError *error = NULL;

    g_variant_get(params, "(^a&s&s)", &package_ids, &directory);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_DOWNLOAD);

    /* Build array of package names */
    GVariantBuilder builder;
    g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));

    for (guint i = 0; package_ids[i] != NULL; i++) {
        g_auto(GStrv) parts = pk_package_id_split(package_ids[i]);
        if (parts != NULL)
            g_variant_builder_add(&builder, "s", parts[PK_PACKAGE_ID_NAME]);
    }

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "DownloadPackages",
        g_variant_new("(@ass)", g_variant_builder_end(&builder), directory),
        G_DBUS_CALL_FLAGS_NONE,
        600000,  /* 10 min timeout */
        NULL,
        &error
    );

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_PACKAGE_DOWNLOAD_FAILED,
                                  "Download failed: %s", error->message);
        g_error_free(error);
        pk_backend_job_finished(job);
        return;
    }

    const gchar *json_str;
    g_variant_get(result, "(&s)", &json_str);

    JsonParser *parser = json_parser_new();
    if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
        JsonNode *root = json_parser_get_root(parser);
        if (JSON_NODE_HOLDS_OBJECT(root)) {
            JsonObject *obj = json_node_get_object(root);
            gboolean success = json_object_get_boolean_member_with_default(obj, "success", FALSE);

            if (success) {
                JsonArray *paths = json_object_get_array_member(obj, "paths");
                if (paths) {
                    guint len = json_array_get_length(paths);
                    for (guint i = 0; i < len && package_ids[i] != NULL; i++) {
                        const gchar *path = json_array_get_string_element(paths, i);
                        if (path)
                            pk_backend_job_files(job, package_ids[i], (gchar *[]){(gchar *)path, NULL});
                    }
                }
            } else {
                const gchar *err_msg = json_object_get_string_member_with_default(obj, "error", "Unknown error");
                pk_backend_job_error_code(job, PK_ERROR_ENUM_PACKAGE_DOWNLOAD_FAILED,
                                          "Download failed: %s", err_msg);
            }
        }
    }
    g_object_unref(parser);
    g_variant_unref(result);

    pk_backend_job_finished(job);
}

void
pk_backend_download_packages(PkBackend *backend, PkBackendJob *job,
                             gchar **package_ids, const gchar *directory)
{
    pk_backend_job_thread_create(job, pk_backend_download_packages_thread, NULL, NULL);
}

static void
pk_backend_install_files_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield transaction_flags;
    gchar **full_paths;
    GError *error = NULL;

    g_variant_get(params, "(t^a&s)", &transaction_flags, &full_paths);

    /* Check for simulate flag */
    gboolean simulate = pk_bitfield_contain(transaction_flags, PK_TRANSACTION_FLAG_ENUM_SIMULATE);
    if (simulate) {
        /* Just validate files exist and return success */
        for (guint i = 0; full_paths[i] != NULL; i++) {
            if (!g_file_test(full_paths[i], G_FILE_TEST_EXISTS)) {
                pk_backend_job_error_code(job, PK_ERROR_ENUM_FILE_NOT_FOUND,
                                          "File not found: %s", full_paths[i]);
                pk_backend_job_finished(job);
                return;
            }
        }
        pk_backend_job_finished(job);
        return;
    }

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_INSTALL);

    /* Build array of paths */
    GVariantBuilder builder;
    g_variant_builder_init(&builder, G_VARIANT_TYPE("as"));
    for (guint i = 0; full_paths[i] != NULL; i++) {
        g_variant_builder_add(&builder, "s", full_paths[i]);
    }

    GVariant *result = g_dbus_proxy_call_sync(
        priv->proxy,
        "InstallFiles",
        g_variant_new("(@as)", g_variant_builder_end(&builder)),
        G_DBUS_CALL_FLAGS_NONE,
        600000,  /* 10 min timeout */
        NULL,
        &error
    );

    if (result == NULL) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_TRANSACTION_ERROR,
                                  "Install failed: %s", error->message);
        g_error_free(error);
        pk_backend_job_finished(job);
        return;
    }

    const gchar *json_str;
    g_variant_get(result, "(&s)", &json_str);

    JsonParser *parser = json_parser_new();
    if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
        JsonNode *root = json_parser_get_root(parser);
        if (JSON_NODE_HOLDS_OBJECT(root)) {
            JsonObject *obj = json_node_get_object(root);
            gboolean success = json_object_get_boolean_member_with_default(obj, "success", FALSE);

            if (!success) {
                const gchar *err_msg = json_object_get_string_member_with_default(obj, "error", "Unknown error");
                pk_backend_job_error_code(job, PK_ERROR_ENUM_TRANSACTION_ERROR,
                                          "Install failed: %s", err_msg);
            }
        }
    }
    g_object_unref(parser);
    g_variant_unref(result);

    pk_backend_job_finished(job);
}

void
pk_backend_install_files(PkBackend *backend, PkBackendJob *job,
                         PkBitfield transaction_flags, gchar **full_paths)
{
    pk_backend_job_thread_create(job, pk_backend_install_files_thread, NULL, NULL);
}

void
pk_backend_what_provides(PkBackend *backend, PkBackendJob *job,
                         PkBitfield filters, gchar **values)
{
    /* Use search with provides flag */
    pk_backend_job_thread_create(job, pk_backend_search_thread, NULL, NULL);
}

void
pk_backend_search_groups(PkBackend *backend, PkBackendJob *job,
                         PkBitfield filters, gchar **values)
{
    /* RPM groups don't map well to PackageKit groups - return empty */
    pk_backend_job_finished(job);
}

static void
pk_backend_search_files_thread(PkBackendJob *job, GVariant *params, gpointer user_data)
{
    PkBitfield filters;
    gchar **values;
    GError *error = NULL;

    g_variant_get(params, "(t^a&s)", &filters, &values);

    if (!ensure_connection(job, &error)) {
        pk_backend_job_error_code(job, PK_ERROR_ENUM_CANNOT_GET_LOCK,
                                  "Cannot connect to urpm D-Bus service: %s",
                                  error->message);
        g_error_free(error);
        return;
    }

    pk_backend_job_set_status(job, PK_STATUS_ENUM_QUERY);

    /* Search for each pattern */
    for (guint i = 0; values[i] != NULL; i++) {
        GVariant *result = g_dbus_proxy_call_sync(
            priv->proxy,
            "SearchFiles",
            g_variant_new("(s)", values[i]),
            G_DBUS_CALL_FLAGS_NONE,
            30000,
            NULL,
            &error
        );

        if (result == NULL) {
            g_warning("SearchFiles failed: %s", error->message);
            g_clear_error(&error);
            continue;
        }

        const gchar *json_str;
        g_variant_get(result, "(&s)", &json_str);

        /* Parse results and emit packages */
        JsonParser *parser = json_parser_new();
        if (json_parser_load_from_data(parser, json_str, -1, NULL)) {
            JsonNode *root = json_parser_get_root(parser);
            if (JSON_NODE_HOLDS_ARRAY(root)) {
                JsonArray *arr = json_node_get_array(root);
                guint len = json_array_get_length(arr);

                /* Track emitted packages to avoid duplicates */
                GHashTable *seen = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);

                for (guint j = 0; j < len; j++) {
                    JsonObject *file_info = json_array_get_object_element(arr, j);
                    if (file_info == NULL)
                        continue;

                    const gchar *pkg_nevra = json_object_get_string_member_with_default(
                        file_info, "pkg_nevra", "");

                    /* Skip if already emitted */
                    if (g_hash_table_contains(seen, pkg_nevra))
                        continue;
                    g_hash_table_add(seen, g_strdup(pkg_nevra));

                    /* Parse NEVRA: name-version-release.arch */
                    g_autofree gchar *nevra_copy = g_strdup(pkg_nevra);
                    gchar *arch_sep = g_strrstr(nevra_copy, ".");
                    if (!arch_sep) continue;
                    *arch_sep = '\0';
                    const gchar *arch = arch_sep + 1;

                    gchar *rel_sep = g_strrstr(nevra_copy, "-");
                    if (!rel_sep) continue;
                    *rel_sep = '\0';
                    const gchar *release = rel_sep + 1;

                    gchar *ver_sep = g_strrstr(nevra_copy, "-");
                    if (!ver_sep) continue;
                    *ver_sep = '\0';
                    const gchar *version = ver_sep + 1;
                    const gchar *name = nevra_copy;

                    g_autofree gchar *evr = g_strdup_printf("%s-%s", version, release);
                    g_autofree gchar *package_id = pk_package_id_build(name, evr, arch, "urpm");

                    pk_backend_job_package(job, PK_INFO_ENUM_AVAILABLE, package_id, "");
                }

                g_hash_table_destroy(seen);
            }
        }
        g_object_unref(parser);
        g_variant_unref(result);
    }

    pk_backend_job_finished(job);
}

void
pk_backend_search_files(PkBackend *backend, PkBackendJob *job,
                        PkBitfield filters, gchar **values)
{
    pk_backend_job_thread_create(job, pk_backend_search_files_thread, NULL, NULL);
}
