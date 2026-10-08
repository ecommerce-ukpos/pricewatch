// Returns the caller's approximate location from Vercel's edge headers. No secrets needed.
export default (req, res) => {
  const h = req.headers;
  let city = h['x-vercel-ip-city'] || '';
  try { city = decodeURIComponent(city); } catch (e) {}
  res.setHeader('Cache-Control', 'no-store');
  res.json({ city, region: h['x-vercel-ip-country-region'] || '', country: h['x-vercel-ip-country'] || '' });
};
