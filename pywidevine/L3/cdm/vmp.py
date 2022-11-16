try:
  from google.protobuf.internal.decoder import _DecodeVarint as _di # this was tested to work with protobuf 3, but it's an internal API (any varint decoder might work)
except ImportError:
  # this is generic and does not depend on pb internals, however it will decode "larger" possible numbers than pb decoder which has them fixed
  def LEB128_decode(buffer, pos, limit = 64):
    result = 0
    shift = 0
    while True:
      b = buffer[pos]
      pos += 1  
      result |= ((b & 0x7F) << shift)
      if not (b & 0x80): 
         return (result, pos)
      shift += 7
      if shift > limit: 
         raise Exception("integer too large, shift: {}".format(shift))
  _di = LEB128_decode


class FromFileMixin:
  @classmethod
  def from_file(cls, filename):
    """Load given a filename"""
    with open(filename,"rb") as f:
      return cls(f.read())

# the signatures use a format internally similar to 
# protobuf's encoding, but without wire types
class VariableReader(FromFileMixin):
  """Protobuf-like encoding reader"""

  def __init__(self, buf):
      self.buf = buf
      self.pos = 0
      self.size = len(buf)
 
  def read_int(self):
    """Read a variable length integer"""
    # _DecodeVarint will take care of out of range errors
    (val, nextpos) = _di(self.buf, self.pos)
    self.pos = nextpos
    return val

  def read_bytes_raw(self, size):      
    """Read size bytes"""
    b = self.buf[self.pos:self.pos+size]
    self.pos += size
    return b

  def read_bytes(self):
    """Read a bytes object""" 
    size = self.read_int()
    return self.read_bytes_raw(size)

  def is_end(self):
    return (self.size == self.pos)

   
class TaggedReader(VariableReader):
  """Tagged reader, needed for implementing a WideVine signature reader"""

  def read_tag(self):
    """Read a tagged buffer"""
    return (self.read_int(), self.read_bytes())

  def read_all_tags(self, max_tag=3):
      tags = {}
      while (not self.is_end()):
        (tag, bytes) = self.read_tag()
        if (tag > max_tag):
           raise IndexError("tag out of bound: got {}, max {}".format(tag, max_tag))

        tags[tag] = bytes
      return tags

class WideVineSignatureReader(FromFileMixin):
  """Parses a widevine .sig signature file."""

  SIGNER_TAG = 1
  SIGNATURE_TAG = 2
  ISMAINEXE_TAG = 3

  def __init__(self, buf):
      reader = TaggedReader(buf)
      self.version = reader.read_int()
      if (self.version != 0):
         raise Exception("Unsupported signature format version {}".format(self.version))
      self.tags = reader.read_all_tags()

      self.signer = self.tags[self.SIGNER_TAG]
      self.signature = self.tags[self.SIGNATURE_TAG]

      extra = self.tags[self.ISMAINEXE_TAG]
      if (len(extra) != 1 or (extra[0] > 1)):
         raise Exception("Unexpected 'ismainexe' field value (not '\\x00' or '\\x01'), please check: {0}".format(extra))
      
      self.mainexe = bool(extra[0])

  @classmethod
  def get_tags(cls, filename):
    """Return a dictionary of each tag in the signature file"""
    return cls.from_file(filename).tags
