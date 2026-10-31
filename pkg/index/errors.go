package index

// Error is a sentinel error type. Sentinels are declared as constants rather
// than package-level variables so the package holds no global mutable state.
// Match them with errors.Is.
type Error string

func (e Error) Error() string { return string(e) }

const (
	// ErrCorrupt reports a malformed, truncated, or checksum-failing segment,
	// manifest, or encoded block.
	ErrCorrupt = Error("index: corrupt data")
	// ErrClosed reports use of a closed Index or Writer.
	ErrClosed = Error("index: closed")
	// ErrInvalidArgument reports a caller-supplied argument outside its contract.
	ErrInvalidArgument = Error("index: invalid argument")
	// ErrDocTooLarge reports a single document whose buffered postings would
	// exceed WriterOptions.MaxBufferBytes on their own.
	ErrDocTooLarge = Error("index: document exceeds writer memory limit")
	// ErrEmptyDocument reports a chunk that produced no indexable tokens.
	ErrEmptyDocument = Error("index: document has no indexable tokens")
	// ErrIndexExists reports that a Writer target directory already holds a
	// published index.
	ErrIndexExists = Error("index: directory already contains an index")
	// ErrWriterLocked reports that another Writer (in this or another
	// process) holds the directory's writer lock.
	ErrWriterLocked = Error("index: directory is locked by another writer")
	// ErrAbandonedBuild reports unpublished segment or temporary files left
	// by an earlier build that did not publish a manifest (crash, failure
	// without Abort). Remove them with CleanAbandoned.
	ErrAbandonedBuild = Error("index: directory contains files from an abandoned build")
	// ErrWriterFailed reports use of a Writer after an earlier fatal error.
	ErrWriterFailed = Error("index: writer failed earlier")
	// ErrUnsupportedPlatform reports that memory-mapped segments are not
	// available on this GOOS.
	ErrUnsupportedPlatform = Error("index: mmap not supported on this platform")
)
