/* Criteria evaluation for the browser. The same rules as src/equity_screener/criteria.py; tests/criteria_cases.json
   holds cases both implementations must agree on (pytest runs these through Node). */
(function (root) {
  'use strict';
  var OPS = {
    '<': function (a, b) { return a < b; },
    '<=': function (a, b) { return a <= b; },
    '>': function (a, b) { return a > b; },
    '>=': function (a, b) { return a >= b; },
  };
  // fail if any criterion is not met; otherwise insufficient_data if any metric is unavailable; otherwise pass
  function evaluate(criteria, metrics) {
    var results = criteria.map(function (c) {
      var actual = metrics[c.metric];
      var status = actual == null ? 'insufficient_data' : (OPS[c.op](actual, c.value) ? 'pass' : 'fail');
      return { criterion: c, actual: actual == null ? null : actual, status: status };
    });
    var statuses = results.map(function (r) { return r.status; });
    var overall = statuses.indexOf('fail') >= 0 ? 'fail' : statuses.indexOf('insufficient_data') >= 0 ? 'insufficient_data' : 'pass';
    return { status: overall, results: results };
  }
  var api = { evaluate: evaluate, OPS: OPS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Criteria = api;
})(typeof window !== 'undefined' ? window : globalThis);
