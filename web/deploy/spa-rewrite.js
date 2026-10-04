// CloudFront Function (viewer request, cloudfront-js-2.0) for the site distribution's default behavior.
// Page routes have no file extension (/, /site/USGS-01427510, /preview/<name>/site/...); they are served the
// app's index.html, so it is the one cached copy and the only path deploys invalidate. Files (assets, data,
// favicon) pass through. /data/* and /api/* are expected on their own behaviors, but are left alone here too.
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri.startsWith('/api/') || uri.startsWith('/data/')) return request;
  var last = uri.split('/').pop();
  if (last.indexOf('.') !== -1) return request;
  var preview = uri.match(/^\/preview\/([a-z0-9][a-z0-9-]*)(\/|$)/);
  request.uri = preview ? '/preview/' + preview[1] + '/index.html' : '/index.html';
  return request;
}
