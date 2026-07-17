// Conventional Commits config, mirroring ERPNext's commitlint.config.js.
// semantic-release (see .releaserc.json) reads these same commit types to decide
// the next version:
//   fix:   -> patch (x.y.Z)
//   feat:  -> minor (x.Y.0)
//   build/chore/ci/docs/perf/refactor/revert/style/test -> no release
// A "BREAKING CHANGE:" footer is allowed but (per .releaserc.json) does NOT force
// a major bump — majors are managed manually, mirroring ERPNext.
module.exports = {
	parserPreset: "conventional-changelog-conventionalcommits",
	rules: {
		"subject-empty": [2, "never"],
		"type-case": [2, "always", "lower-case"],
		"type-empty": [2, "never"],
		"type-enum": [
			2,
			"always",
			["build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor", "revert", "style", "test"],
		],
	},
};
