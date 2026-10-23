package main

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// unix.Stat_t exposes nanosecond ctime on both supported hosts, macOS and Linux.
// A same-size edit followed by restoring mtime must invalidate a local hit.
type frozenStamp struct {
	device, inode     uint64
	size              int64
	mode              uint32
	modified, changed unix.Timespec
}

func stamp(s unix.Stat_t) frozenStamp {
	return frozenStamp{uint64(s.Dev), uint64(s.Ino), s.Size, uint32(s.Mode), s.Mtim, s.Ctim}
}

type verifiedFile struct {
	stamp  frozenStamp
	digest string
}

func (g *Guard) verifyFrozen(force bool) error {
	if g.verified == nil {
		g.verified = make(map[string]verifiedFile)
	}
	for path, want := range g.c.Frozen {
		if err := noLinks(path); err != nil {
			return err
		}
		var before unix.Stat_t
		if err := unix.Lstat(path, &before); err != nil {
			return err
		}
		if before.Mode&unix.S_IFMT != unix.S_IFREG {
			return errors.New("frozen evidence must be a regular file")
		}
		expected := verifiedFile{stamp(before), want}
		if !force && g.verified[path] == expected {
			continue
		}
		delete(g.verified, path)
		if err := hashFrozen(path, want, expected.stamp); err != nil {
			return err
		}
		g.verified[path] = expected
	}
	return nil
}

func hashFrozen(path, want string, before frozenStamp) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	var opened, after, current unix.Stat_t
	if err = unix.Fstat(int(file.Fd()), &opened); err != nil {
		return err
	}
	if stamp(opened) != before {
		return errors.New("frozen evidence changed while opening")
	}
	h := sha256.New()
	if _, err = io.Copy(h, file); err != nil {
		return err
	}
	if hex.EncodeToString(h.Sum(nil)) != want {
		return errors.New("frozen evidence changed")
	}
	if err = unix.Fstat(int(file.Fd()), &after); err != nil {
		return err
	}
	if err = noLinks(path); err != nil {
		return err
	}
	if err = unix.Lstat(path, &current); err != nil {
		return err
	}
	if stamp(after) != before || stamp(current) != before {
		return errors.New("frozen evidence changed while hashing")
	}
	return nil
}
