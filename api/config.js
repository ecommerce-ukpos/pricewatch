export default async (req, res) => {
  return res.json({
    supabase_url: process.env.SUPABASE_URL,
    supabase_anon_key: process.env.SUPABASE_ANON_KEY,
    allowed_domains: (process.env.ALLOWED_EMAIL_DOMAINS || "ukpos.com").split(",").map(d => d.trim().toLowerCase())
  });
};
