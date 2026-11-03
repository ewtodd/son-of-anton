package store

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// PhysicsRun is one row of physics_runs: a physics run (phase 11c).
type PhysicsRun struct {
	ID, Name        string
	SpecPath        string // the spec with its expected values, outside the run directory
	RunDir          string
	Worker, Manager string // session ids
	// State is working, reviewing, passed, failed, stopped or interrupted;
	// Phase is the state an interrupted run was in (what resume continues).
	State, Phase string
	SessionsDone int
	Tokens       int // tokens used, both sessions together
	WorkerModel  string
	ManagerModel string
	Limits       string  // JSON of the limits in force
	Elapsed      float64 // seconds the run has been going, earlier activations included
	// NextMessage starts the next work session; WorkStarted says it was sent.
	NextMessage string
	WorkStarted bool
	Started     time.Time
	Ended       time.Time
}

// PhysicsActive says whether the run is going (working or reviewing).
func (r PhysicsRun) PhysicsActive() bool { return r.State == "working" || r.State == "reviewing" }

const physicsCols = `id, name, spec_path, run_dir, worker_session, manager_session, state, phase, sessions_done, tokens_used,
	worker_model, manager_model, limits, elapsed_s, next_message, work_started, started, ended`

// CreatePhysicsRun stores a new run.
func (s *Store) CreatePhysicsRun(ctx context.Context, r PhysicsRun) error {
	_, err := s.db.ExecContext(ctx, `INSERT INTO physics_runs (`+physicsCols+`) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		r.ID, r.Name, r.SpecPath, r.RunDir, r.Worker, r.Manager, r.State, r.Phase, r.SessionsDone, r.Tokens,
		r.WorkerModel, r.ManagerModel, r.Limits, r.Elapsed, r.NextMessage, boolInt(r.WorkStarted), formatTime(r.Started), nullTime(r.Ended))
	return err
}

// SavePhysicsRun writes every mutable field of the run.
func (s *Store) SavePhysicsRun(ctx context.Context, r PhysicsRun) error {
	_, err := s.db.ExecContext(ctx, `UPDATE physics_runs SET state = ?, phase = ?, sessions_done = ?, tokens_used = ?, limits = ?,
		elapsed_s = ?, next_message = ?, work_started = ?, ended = ? WHERE id = ?`,
		r.State, r.Phase, r.SessionsDone, r.Tokens, r.Limits, r.Elapsed, r.NextMessage, boolInt(r.WorkStarted), nullTime(r.Ended), r.ID)
	return err
}

func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

type rowScanner interface{ Scan(dest ...any) error }

func scanPhysicsRun(row rowScanner) (PhysicsRun, error) {
	var r PhysicsRun
	var started string
	var ended sql.NullString
	var ws int
	err := row.Scan(&r.ID, &r.Name, &r.SpecPath, &r.RunDir, &r.Worker, &r.Manager, &r.State, &r.Phase, &r.SessionsDone, &r.Tokens,
		&r.WorkerModel, &r.ManagerModel, &r.Limits, &r.Elapsed, &r.NextMessage, &ws, &started, &ended)
	if errors.Is(err, sql.ErrNoRows) {
		return PhysicsRun{}, ErrNotFound
	}
	if err != nil {
		return PhysicsRun{}, err
	}
	r.WorkStarted = ws != 0
	if r.Started, err = parseTime(started); err != nil {
		return PhysicsRun{}, err
	}
	if ended.Valid {
		if r.Ended, err = parseTime(ended.String); err != nil {
			return PhysicsRun{}, err
		}
	}
	return r, nil
}

// PhysicsRun loads one run by id.
func (s *Store) PhysicsRun(ctx context.Context, id string) (PhysicsRun, error) {
	return scanPhysicsRun(s.db.QueryRowContext(ctx, `SELECT `+physicsCols+` FROM physics_runs WHERE id = ?`, id))
}

// PhysicsRunBySession is the run a worker or manager session belongs to.
func (s *Store) PhysicsRunBySession(ctx context.Context, session string) (PhysicsRun, error) {
	return scanPhysicsRun(s.db.QueryRowContext(ctx, `SELECT `+physicsCols+` FROM physics_runs WHERE worker_session = ? OR manager_session = ?`, session, session))
}

// PhysicsRuns lists the runs, newest first.
func (s *Store) PhysicsRuns(ctx context.Context) ([]PhysicsRun, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+physicsCols+` FROM physics_runs ORDER BY started DESC, id DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PhysicsRun
	for rows.Next() {
		r, err := scanPhysicsRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}
