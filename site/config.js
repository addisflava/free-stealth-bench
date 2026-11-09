/* Public runtime configuration for the static svg-bench leaderboard.
 *
 * Fill in the Supabase project URL and the *anon* key to let every visitor read
 * and submit public reviews. The anon key is safe to publish: it is constrained
 * by the row-level-security policies in svg-bench/supabase.sql. Never put a
 * service_role key here.
 *
 * These values can also be injected at build time:
 *   SUPABASE_URL=... SUPABASE_ANON_KEY=... python3 build_site.py
 */
window.SVG_BENCH_CONFIG = {
  supabase: {
    url: '',
    anonKey: '',
    table: 'svgbench_reviews'
  }
};
