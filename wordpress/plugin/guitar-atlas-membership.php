<?php
/**
 * Plugin Name: GUITAR ATLAS Membership
 * Description: Premium member role sync and content gate for GUITAR ATLAS.
 * Version: 1.0.0
 * Author: 6thMan株式会社
 * License: Proprietary
 */

if (!defined('ABSPATH')) {
    exit;
}

register_activation_hook(__FILE__, 'ga_add_premium_role');
add_filter('the_content', 'ga_gate_premium_content', 20);
add_action('rest_api_init', 'ga_register_membership_routes');

/**
 * Register the premium_member role and Premium capabilities.
 *
 * @return void
 */
function ga_add_premium_role(): void {
    add_role('premium_member', 'Premium Member', [
        'read'                  => true,
        'ga_view_premium_index' => true,
        'ga_view_deep_report'   => true,
        'ga_view_alerts'        => true,
    ]);
}

/**
 * Gate posts with premium_required=1 to premium members.
 *
 * @param string $content Post content.
 * @return string Full content for premium users, excerpt plus CTA otherwise.
 */
function ga_gate_premium_content(string $content): string {
    if (!is_singular()) {
        return $content;
    }

    $premium_required = get_post_meta(get_the_ID(), 'premium_required', true);
    if ((string) $premium_required !== '1') {
        return $content;
    }

    if (is_user_logged_in() && current_user_can('ga_view_premium_index')) {
        return $content;
    }

    $excerpt = wp_html_excerpt(wp_strip_all_tags($content), 300, '...');
    $cta = '<div class="ga-premium-cta">'
        . '<h2>Premium 会員限定コンテンツ</h2>'
        . '<p>GUITAR ATLAS Premium に登録すると、全文と詳細レポートを閲覧できます。</p>'
        . '<p><a href="/premium" class="button">Premium を見る</a></p>'
        . '</div>';

    return wpautop($excerpt) . $cta;
}

/**
 * Register REST routes for internal membership sync.
 *
 * @return void
 */
function ga_register_membership_routes(): void {
    register_rest_route('guitar-atlas/v1', '/membership/sync', [
        'methods'             => 'POST',
        'callback'            => 'ga_sync_membership',
        'permission_callback' => 'ga_verify_internal_token',
    ]);
}

/**
 * Verify the internal membership sync token.
 *
 * @param WP_REST_Request $req REST request.
 * @return bool True when the provided token matches the configured token.
 */
function ga_verify_internal_token(WP_REST_Request $req): bool {
    $expected = defined('GA_MEMBERSHIP_INTERNAL_TOKEN') ? GA_MEMBERSHIP_INTERNAL_TOKEN : '';
    if (!$expected) {
        $expected = getenv('GA_MEMBERSHIP_INTERNAL_TOKEN') ?: '';
    }

    $provided = $req->get_header('x_ga_internal_token');
    return (bool) ($expected && hash_equals($expected, $provided ?: ''));
}

/**
 * Sync Premium membership role for a WordPress user.
 *
 * @param WP_REST_Request $req REST request containing wp_user_id, action, and metadata.
 * @return WP_REST_Response Response with success flag, roles, user ID, and timestamp.
 */
function ga_sync_membership(WP_REST_Request $req): WP_REST_Response {
    $params = $req->get_json_params();
    $wp_user_id = isset($params['wp_user_id']) ? absint($params['wp_user_id']) : 0;
    $action = isset($params['action']) ? sanitize_key($params['action']) : '';
    $stripe_customer_id = isset($params['stripe_customer_id'])
        ? sanitize_text_field($params['stripe_customer_id'])
        : '';
    $reason = isset($params['reason']) ? sanitize_key($params['reason']) : '';

    if (!$wp_user_id || !in_array($action, ['promote', 'demote'], true)) {
        return new WP_REST_Response([
            'success' => false,
            'error'   => 'invalid request',
        ], 400);
    }

    $user = get_user_by('id', $wp_user_id);
    if (!$user instanceof WP_User) {
        return new WP_REST_Response([
            'success'    => false,
            'error'      => 'user not found',
            'wp_user_id' => $wp_user_id,
        ], 404);
    }

    $already = in_array('premium_member', (array) $user->roles, true);
    if ($action === 'promote') {
        $user->add_role('premium_member');
        if ($stripe_customer_id) {
            update_user_meta($wp_user_id, 'ga_stripe_customer_id', $stripe_customer_id);
        }
    } else {
        $user->remove_role('premium_member');
        if ($reason) {
            update_user_meta($wp_user_id, 'ga_membership_demote_reason', $reason);
        }
    }

    $fresh_user = get_user_by('id', $wp_user_id);
    $roles = $fresh_user instanceof WP_User ? array_values((array) $fresh_user->roles) : [];

    return new WP_REST_Response([
        'success'    => true,
        'new_roles'  => $roles,
        'wp_user_id' => $wp_user_id,
        'updated_at' => gmdate('c'),
        'already'    => $already && $action === 'promote',
    ], 200);
}

add_shortcode('ga_dashboard_link', 'ga_render_dashboard_link');

/**
 * Base64url encode raw bytes for dashboard HMAC token compatibility.
 *
 * @param string $data Raw bytes.
 * @return string URL-safe base64 without padding.
 */
function ga_dashboard_base64url_encode(string $data): string {
    return rtrim(strtr(base64_encode($data), '+/', '-_'), '=');
}

/**
 * Issue a short-lived dashboard token compatible with dashboard.token.verify_token().
 *
 * @param int $wp_user_id WordPress user ID.
 * @param array $roles User roles.
 * @param int $ttl Token lifetime in seconds.
 * @return string Token, or empty string when the shared secret is not configured.
 */
function ga_issue_dashboard_token(int $wp_user_id, array $roles, int $ttl = 1800): string {
    $secret = defined('DASHBOARD_HMAC_SECRET') ? DASHBOARD_HMAC_SECRET : '';
    if (!$secret) {
        $secret = getenv('DASHBOARD_HMAC_SECRET') ?: '';
    }
    if (!$secret) {
        return '';
    }

    $payload = [
        'exp' => time() + $ttl,
        'r'   => array_values(array_map('strval', $roles)),
        'u'   => $wp_user_id,
    ];
    $json = wp_json_encode($payload, JSON_UNESCAPED_SLASHES);
    $payload_b64 = ga_dashboard_base64url_encode($json);
    $sig_b64 = ga_dashboard_base64url_encode(hash_hmac('sha256', $payload_b64, $secret, true));
    return $payload_b64 . '.' . $sig_b64;
}

/**
 * Render the Premium dashboard link shortcode.
 *
 * @return string Dashboard link for Premium members, or Premium signup CTA.
 */
function ga_render_dashboard_link(): string {
    if (!is_user_logged_in()) {
        return '<a class="ga-dashboard-link" href="' . esc_url(home_url('/premium')) . '">' . esc_html__('Premium 登録へ', 'guitar-atlas') . '</a>';
    }

    $user = wp_get_current_user();
    $roles = array_values((array) $user->roles);
    if (!in_array('premium_member', $roles, true) && !current_user_can('ga_view_premium_index')) {
        return '<a class="ga-dashboard-link" href="' . esc_url(home_url('/premium')) . '">' . esc_html__('Premium 登録へ', 'guitar-atlas') . '</a>';
    }

    $token = ga_issue_dashboard_token((int) $user->ID, $roles);
    if (!$token) {
        return '<a class="ga-dashboard-link" href="' . esc_url(home_url('/account')) . '">' . esc_html__('アカウントへ戻る', 'guitar-atlas') . '</a>';
    }

    $base_url = defined('DASHBOARD_BASE_URL') ? DASHBOARD_BASE_URL : '';
    if (!$base_url) {
        $base_url = getenv('DASHBOARD_BASE_URL') ?: 'https://theguitaratlas.com/dashboard';
    }
    $separator = strpos($base_url, '?') !== false ? '&' : '?';
    $url = $base_url . $separator . 't=' . rawurlencode($token);

    return '<a class="ga-dashboard-link" href="' . esc_url($url) . '">' . esc_html__('Premium Dashboard を開く', 'guitar-atlas') . '</a>';
}
