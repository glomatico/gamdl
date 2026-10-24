//! The single native extension for Gamdl's low-level MP4 operations.

use pyo3::prelude::*;

mod decrypt;
mod stream;
mod tags;
mod wrapper;

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(tags::write_mp4_tags, m)?)?;
    m.add_class::<stream::NativeStream>()?;
    Ok(())
}
